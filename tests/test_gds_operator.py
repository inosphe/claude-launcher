"""The gds6 operator: a second workflow on operator v2's surface.

What has to hold (claunch-eyz0s):
  - ``gds-operator-v2`` compiles with no finding but the declared
    ``unbounded-visits`` (the same service loop operator-v2 runs), and its one
    ``observe`` task carries the gds6 probes, the stage order and the nudge
    rules — the operator nudges from the ledger's values, not from memory;
  - the pack ``gds6-operator`` ships in the package, is not on-demand, and
    says the same thing (stage order, read-only, the two question channels);
  - an operator record names its workflow (``operator-v2`` by default),
    whether its session keeps QA, and the gds6 address; an unknown workflow
    is a 400 rather than a run that half-works;
  - starting an operator uses that workflow, gives a gds6 operator its pack
    and ``gds6_url`` input, and carries the QA switch to the MCP server's
    environment — a standard operator's spec is unchanged.
"""
from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import mcp_server, prompt_packs, qa_mcp, session_env
from claude_launcher.cflow2 import check, layers
from claude_launcher.daemon import operators_v2


def _chart():
    return layers.load("gds-operator-v2")


# -- definition ---------------------------------------------------------------
def test_compiles_with_only_the_declared_unbounded_visits():
    assert [f.code for f in check.check(_chart())] == ["unbounded-visits"]


def test_the_loop_is_operator_v2s_with_a_ledger_pass():
    chart = _chart()
    labels = {n.label for n in chart.nodes.values() if n.label}
    assert {"greet", "service", "watch-gate", "observe", "rest",
            "probe-failed", "probe-backoff"} <= labels
    inputs = chart.meta["inputs"]
    assert inputs["operator"]["required"] is True
    assert inputs["gds6_url"]["default"] == "http://localhost:8888"


def test_the_observe_task_reads_the_ledger_and_nudges_from_it(tmp_path):
    """The probes, the order and the nudge rules ride the one task that acts."""
    from claude_launcher.cflow2 import runtime

    st = runtime.start(_chart(), cwd=str(tmp_path), scope="op", caps={},
                       inputs={"operator": "0123456789ab", "gds6_url": "http://gds:8888"}, now=1_000_000.0)
    assert [t["label"] for t in st["tasks"]] == ["greet"]
    inst = [t["instance"] for t in st["tasks"] if t["label"] == "greet"][0]
    runtime.report(inst, "ok", {"handled": 0}, cwd=str(tmp_path), scope="op", now=1_000_001.0)

    def runner(probe, cwd):
        return (0, "")

    st = runtime.wait_tick(1_000_040.0, runner, cwd=str(tmp_path), scope="op")
    task = [t for t in st["tasks"] if t["label"] == "observe"][0]
    prompt = task["prompt"]
    rules = {r["id"] for r in task["rules"]}

    # every stage the operator nudges along is read from the server, not guessed
    for probe in ("/api/plane/progress", "/api/plane/frames", "/api/frames/summary",
                  "/api/delivery?frame_id=", "/api/beads?frame_id=", "/api/scan",
                  "/api/impl-decisions?frame_id="):
        assert probe in prompt, probe
    # A planning frame with no realization history is absent from the
    # summary (issue-gen only lists frames found in works/issues/runs), so
    # the list is read beside it and an empty summary is not "nothing to do".
    assert "실현 이력이 있는 프레임만 담는다" in prompt
    assert "`frames: {}`를 「할 일 없음」으로 읽지 않는다" in prompt
    assert "http://gds:8888" in prompt  # the run's own address, not a constant
    assert {"stage-order", "evidence-before-nudge", "review-before-nudge",
            "one-action-per-post", "qa-when", "no-decisions"} <= rules
    assert {"inbox-anytime", "dispatch-on-behalf", "existing-session-first",
            "markdown-body"} <= rules
    # the review and nudge shapes are in the task, not only in a pack
    assert "30분" in prompt and "이전→지금" in prompt
    assert "opv2_ask" in prompt and "qa_ask" in prompt


# -- the pack -----------------------------------------------------------------
def test_the_pack_ships_with_the_stage_order_and_the_read_only_rule():
    pack = prompt_packs.shipped("gds6-operator")
    assert not prompt_packs.is_on_demand("gds6-operator")
    body = pack.body
    for key in ("extract", "anchors", "catalog", "review", "scenarios", "decisions", "frames"):
        assert f"`{key}`" in body, key
    assert "읽기만" in body and "쓰기" in body
    assert "opv2_ask" in body and "qa_ask" in body
    assert "POST·PATCH·PUT·DELETE" in body
    # the frame rule the review found missing: the summary is a subset
    assert "/api/plane/frames" in body and "/api/frames/summary" in body
    assert "`frames: {}`인 채로" in body


def test_the_gds_workflow_names_the_pack_and_the_pack_ships():
    assert operators_v2.WORKFLOW_PACKS[operators_v2.GDS_WORKFLOW] == ("gds6-operator",)
    ids = {p.id for p in prompt_packs.catalog()}
    assert "gds6-operator" in ids


# -- the record ---------------------------------------------------------------
class FakeSession:
    def __init__(self, name, project="gds", env=None):
        self.sdef = SimpleNamespace(name=name, project=project, cwd="/repo/gds", task="t",
                                    issue=None, env=dict(env or {}))
        self.exited = False

    def info(self):
        return {"status": "idle"}


class FakeManager:
    def __init__(self):
        self.sessions = {}

    def list(self):
        return list(self.sessions.values())

    def kill(self, name, force=False, by=None):
        self.sessions[name].exited = True
        return self.sessions[name]

    def set_keep_alive(self, name, on):
        return None


@pytest.fixture
def world(tmp_path, monkeypatch):
    def require(name):
        return SimpleNamespace(name=name)

    monkeypatch.setattr(operators_v2.projects, "require", require)
    monkeypatch.setattr(operators_v2.projects, "default_cwd", lambda name, doc=None: None)
    manager = FakeManager()
    return SimpleNamespace(ops=operators_v2.OperatorsV2(tmp_path, manager, None), manager=manager, root=tmp_path)


def test_a_record_defaults_to_operator_v2_and_names_the_three_fields(world):
    standard = world.ops.create({"name": "Watch", "all": True, "profile": "p"})
    assert (standard["workflow"], standard["qa"], standard["gds6_url"]) == ("operator-v2", False, None)
    row = world.ops.summary(standard)
    assert row["workflow"] == "operator-v2" and row["qa"] is False and row["gds6_url"] is None

    gds = world.ops.create({"name": "Guide", "projects": ["gds"], "workflow": "gds-operator-v2",
                            "qa": True, "gds6_url": "http://gds:8888"})
    row = world.ops.summary(gds)
    assert row["workflow"] == "gds-operator-v2" and row["qa"] is True
    assert row["gds6_url"] == "http://gds:8888"

    with pytest.raises(ValueError):
        world.ops.create({"name": "Typo", "projects": ["gds"], "workflow": "gds-operator"})
    with pytest.raises(ValueError):
        world.ops.create({"name": "Loose", "projects": ["gds"], "qa": "yes"})


def test_the_qa_switch_is_read_from_the_record(world):
    assert world.ops.qa_allowed("nobody") is False
    gds = world.ops.create({"name": "Guide", "projects": ["gds"], "workflow": "gds-operator-v2", "qa": True})
    standard = world.ops.create({"name": "Watch", "projects": ["gds"]})
    world.ops.bind(gds, "op-gds")
    world.ops.bind(standard, "op-watch")
    assert world.ops.qa_allowed("op-gds") is True
    assert world.ops.qa_allowed("op-watch") is False
    assert world.ops.operator_of_session("op-gds")["id"] == gds["id"]


def test_a_qa_toggle_applies_from_the_next_start_not_now(world):
    """Both directions: the running session keeps what it started with.

    The MCP surface is fixed at process start, so the daemon has to answer
    from the same value or the three doors disagree (claunch-eyz0s review:
    ``qa_*`` stayed on a surface whose API and pane had just been turned
    off, and the reverse left the tools hidden while the pane opened).
    """
    on = world.ops.create({"name": "Guide", "projects": ["gds"],
                           "workflow": "gds-operator-v2", "qa": True})
    world.ops.bind(on, "op-on")
    world.manager.sessions["op-on"] = FakeSession("op-on", env={session_env.OPERATOR_QA_ENV: "1"})
    assert world.ops.qa_allowed("op-on") is True

    asyncio.run(world.ops.update(on["id"], {"qa": False}))
    assert world.ops.summary(on)["qa"] is False
    assert world.ops.qa_allowed("op-on") is True, "the running session keeps its surface"
    world.manager.sessions["op-on"] = FakeSession("op-on")      # restarted, switch now off
    assert world.ops.qa_allowed("op-on") is False

    off = world.ops.create({"name": "Watch", "projects": ["gds"],
                            "workflow": "gds-operator-v2", "qa": False})
    world.ops.bind(off, "op-off")
    world.manager.sessions["op-off"] = FakeSession("op-off")
    assert world.ops.qa_allowed("op-off") is False
    asyncio.run(world.ops.update(off["id"], {"qa": True}))
    assert world.ops.summary(off)["qa"] is True
    assert world.ops.qa_allowed("op-off") is False, "...and no tools appear behind its back"
    world.manager.sessions["op-off"] = FakeSession("op-off", env={session_env.OPERATOR_QA_ENV: "1"})
    assert world.ops.qa_allowed("op-off") is True


def test_update_switches_the_workflow_and_says_it_applies_next_start(world):
    rec = world.ops.create({"name": "Guide", "projects": ["gds"]})
    out = asyncio.run(world.ops.update(rec["id"], {"workflow": "gds-operator-v2", "qa": True}))
    assert out["workflow"] == "gds-operator-v2" and out["qa"] is True
    feed = [e["text"] for e in world.ops.feed(rec)]
    assert any("워크플로" in line and "다음 시작부터" in line for line in feed)


# -- start --------------------------------------------------------------------
def _app(world, created):
    app = web.Application()
    app["manager"] = world.manager

    async def create(request, body):
        created.append(body)
        world.manager.sessions["op-new"] = FakeSession("op-new")
        return 201, {"name": "op-new"}

    operators_v2.install(app, create=create)
    app["operators_v2"].root = world.root / "operators"
    app["operators_v2"].cache = None
    return app


def _start(world, body):
    created = []

    async def go():
        async with TestClient(TestServer(_app(world, created))) as client:
            rec = await (await client.post("/api/operators", json=body)).json()
            resp = await client.post(f"/api/operators/{rec['id']}/start", json={"profile": "p"})
            assert resp.status == 201, await resp.text()

    asyncio.run(go())
    return created[0]


def test_a_gds6_operator_starts_its_workflow_with_the_pack_and_the_qa_switch(world):
    spec = _start(world, {"name": "Guide", "projects": ["gds"], "workflow": "gds-operator-v2",
                          "qa": True, "gds6_url": "http://gds:8888"})
    assert spec["workflow"] == "gds-operator-v2"
    assert spec["cflow_inputs"] == {"operator": spec["cflow_inputs"]["operator"], "gds6_url": "http://gds:8888"}
    assert spec["prompt_packs"] == ["gds6-operator"]
    assert spec["env"] == {session_env.OPERATOR_ENV: spec["env"][session_env.OPERATOR_ENV],
                           session_env.OPERATOR_QA_ENV: "1"}
    assert "gds6" in spec["task"] and "http://gds:8888" in spec["task"]


def test_a_standard_operator_keeps_its_spec(world):
    spec = _start(world, {"name": "Watch", "all": True})
    assert spec["workflow"] == "operator-v2" == operators_v2.WORKFLOW
    assert "prompt_packs" not in spec
    assert spec["env"] == {session_env.OPERATOR_ENV: spec["env"][session_env.OPERATOR_ENV]}
    assert "gds6" not in spec["task"]


# -- the surface --------------------------------------------------------------
def test_the_rail_says_which_sessions_are_operators_and_whether_qa_is_open():
    """The header's 🗪 threads and ? qa buttons read the rail.

    An operator's session has no session panes, except QA when its record
    keeps it; the rail carries both answers so the page does not have to
    guess from the workflow name (``daemon/api.py``
    ``_operator_rail_fields``).
    """
    from claude_launcher.daemon import api as api_mod

    class Store:
        def __init__(self, bound, qa):
            self.bound, self.qa = bound, qa

        def operator_of_session(self, name):
            return {"id": "x"} if name in self.bound else None

        def qa_allowed(self, name):
            return name in self.qa

    rows = {"op": {"workflow": "gds-operator-v2"}, "op2": {"workflow": "operator-v2"},
            "plain": {"workflow": "improv-worker-v2"}}
    api_mod._operator_rail_fields({"operators_v2": Store({"op", "op2"}, {"op"})}, rows)
    assert rows["op"] == {"workflow": "gds-operator-v2", "operator": True, "operator_qa": True}
    assert rows["op2"] == {"workflow": "operator-v2", "operator": True, "operator_qa": False}
    assert rows["plain"] == {"workflow": "improv-worker-v2"}

    # No store (or one that cannot answer) leaves the rows untouched.
    untouched = {"a": {"workflow": "operator-v2"}}
    assert api_mod._operator_rail_fields({}, untouched) == {"a": {"workflow": "operator-v2"}}


def test_the_mcp_surface_keeps_qa_for_an_operator_that_keeps_it(monkeypatch):
    def offered():
        resp = mcp_server.SERVER.handle({"jsonrpc": "2.0", "id": 1, "method": "tools/list"})
        return {t["name"] for t in resp["result"]["tools"]}

    monkeypatch.setenv(session_env.OPERATOR_ENV, "0123456789ab")
    monkeypatch.delenv(session_env.OPERATOR_QA_ENV, raising=False)
    assert not (set(qa_mcp.TOOL_NAMES) & offered())

    monkeypatch.setenv(session_env.OPERATOR_QA_ENV, "1")
    assert set(qa_mcp.TOOL_NAMES) <= offered()
