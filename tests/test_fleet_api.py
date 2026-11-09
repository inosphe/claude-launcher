"""The dashboard's fleet routes (``daemon/fleet_api.py``) over a fake
executor backend: the preset list, the editor's check/save/delete, the
expansion preview and the person-only launch."""

import asyncio

import yaml
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import fleet, workspaces
from claude_launcher.daemon import actors, fleet_api
from test_workspec_import import FakeBackend

LAUNCH = {"task": "로그인 개편", "default_profile": "sr", "name": "fleet-t"}


def _run(scenario, made=None):
    def make(doc, board, *, app, loop, actor):
        fake = FakeBackend()
        if made is not None:
            made.append((doc, board, actor, fake))
        return fake

    async def go():
        app = web.Application()
        fleet_api.install(app, backend=make)
        async with TestClient(TestServer(app)) as client:
            async def call(method, path, body=None, status=200, headers=None):
                response = await client.request(method, "/api/fleet/" + path,
                                                json=body, headers=headers)
                assert response.status == status, await response.text()
                return await response.json()
            await scenario(call)
    asyncio.run(go())


def _workspace(tmp_path, name="ws"):
    d = tmp_path / name
    d.mkdir()
    return workspaces.add(str(d), name=name).path


def test_the_list_carries_the_builtin_with_its_groups_and_shape(home):
    async def scenario(call):
        out = await call("GET", "presets")
        assert out["default"] == fleet.DEFAULT_NAME
        row = out["presets"][0]
        assert (row["name"], row["source"], row["errors"]) == (fleet.DEFAULT_NAME, "builtin", [])
        assert [g["id"] for g in row["groups"]] == ["coord", "lead", "w"]
        assert len(row["shape"]) == 7
        assert "coordinator" in out["kinds"] and out["issue_modes"] == ["lane", "pending", "none"]
    _run(scenario)


def test_check_save_and_delete_a_preset(home):
    doc = fleet.load(fleet.BUILTIN[fleet.DEFAULT_NAME])
    doc["name"] = "mine"

    async def scenario(call):
        out = await call("POST", "check", {"doc": doc, "params": {"leads": 1, "workers": 1}})
        assert out["errors"] == [] and len(out["shape"]) == 3
        assert yaml.safe_load(out["yaml"])["name"] == "mine"
        bad = {**doc, "root": {**doc["root"], "kind": "reviewer"}}
        assert (await call("POST", "check", {"doc": bad}))["errors"]
        await call("POST", "check", {"yaml": "a: [unclosed"}, 400)
        await call("PUT", "presets/other", {"doc": doc}, 400)       # name vs address
        saved = await call("PUT", "presets/mine", {"doc": doc})
        assert saved["preset"]["source"] == "user"
        names = [p["name"] for p in (await call("GET", "presets"))["presets"]]
        assert "mine" in names
        await call("DELETE", "presets/mine")
        await call("DELETE", "presets/mine", status=404)
    _run(scenario)


def test_expand_previews_the_tree_and_names_the_missing_workspace(home, tmp_path):
    async def scenario(call):
        out = await call("POST", "expand", {"preset": fleet.DEFAULT_NAME, "launch": LAUNCH})
        assert [a["id"] for a in out["agents"]][:2] == ["coord", "lead1"]
        assert out["mesh_mode"] == "new" and out["prefix"] == ""
        assert out["board_error"] and out["lines"]
        _workspace(tmp_path)
        out = await call("POST", "expand", {"preset": fleet.DEFAULT_NAME,
                                            "launch": {**LAUNCH, "workspace": "ws"}})
        assert out["board_error"] == "" and out["board_dir"]
        out = await call("POST", "expand", {"preset": fleet.DEFAULT_NAME,
                                            "launch": {**LAUNCH, "task": ""}}, 400)
        assert out["launch_error"] is True
    _run(scenario)


def test_launch_imports_the_tree_and_refuses_an_agent(home, tmp_path):
    board = _workspace(tmp_path)
    made = []

    async def scenario(call):
        body = {"preset": fleet.DEFAULT_NAME,
                "launch": {**LAUNCH, "workspace": "ws", "params": {"leads": 1, "workers": 1}}}
        agent = {actors.HEADER: actors.header_value(actors.AGENT, "s1")}
        out = await call("POST", "launch", body, 403, headers=agent)
        assert "person" in out["error"]
        await call("POST", "launch", {**body, "launch": LAUNCH}, 400)  # no workspace
        assert made == []
        out = await call("POST", "launch", body)
        assert out["ok"] is True and out["name"] == "fleet-t"
    _run(scenario, made)
    (doc, got_board, actor, fake) = made[0]
    assert got_board == board and actor["kind"] != actors.AGENT
    spawns = [c for c in fake.calls if c[0] == "spawn"]
    assert [(p, b["handle"]) for _, p, b in spawns] == [
        (None, "coord"), ("s-coord", "lead1"), ("s-lead1", "lead1-w1")]
    assert spawns[0][2]["profile"] == "sr"
