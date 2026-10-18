"""Category routes and assign to tree (daemon/category_api.py, category_assign.py).

The conversation copy is stubbed exactly as test_deep_think.py does it
(:func:`spawn.can_fork` / :func:`spawn._fork_parents_conversation`); the fork,
the job record, the agent's assignment, the merge block and the in-place path
run for real against a served daemon.
"""

from __future__ import annotations

import asyncio

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import category, workspaces
from claude_launcher.daemon import category_api, category_assign as ca
from claude_launcher.daemon.harness import SessionDef

from test_deep_think import BEARER, _register_py_harness, _serve, _mgr, _typed_to, forkable, typed  # noqa: F401
from claude_launcher.daemon.mesh import MeshManager


def _run(scenario, tmp_path, issue="claunch-x1"):
    _register_py_harness()
    workspaces.add(str(tmp_path), "alpha")

    async def run():
        mgr = _mgr()
        mm = MeshManager(mgr, root=tmp_path / "mesh")
        client = await _serve(mgr, mm)
        try:
            mgr.create(SessionDef(name="a", harness="py", cwd=str(tmp_path), issue=issue))
            await scenario(mgr, client)
            await mgr.shutdown_all()
        finally:
            await client.close()

    asyncio.run(run())


async def _call(client, method, path, body=None, status=200):
    resp = await client.request(method, path, json=body, headers=BEARER)
    doc = await resp.json()
    assert resp.status == status, doc
    return doc


def test_the_task_block_scopes_a_fork_and_an_in_place_session():
    items = [{"kind": "session", "ref": "a"}, {"kind": "issue", "ref": "claunch-1"}]
    fork = ca.compose_task(origin="a", workspace="alpha", items=items, fork="a-cat1")
    assert fork.startswith("---\n# claunch category: ASSIGN TO TREE")
    assert "items: session a, issue claunch-1" in fork
    assert "quick-fork copy of a" in fork and "'handoff' tool" in fork
    assert "'category_assign' exactly once" in fork
    here = ca.compose_task(origin="a", workspace="alpha", items=items, fork=None)
    assert "carry on with what you were doing" in here and "'handoff'" not in here


def test_assign_forks_the_session_and_the_merge_reports_the_category(home, tmp_path, forkable, typed):
    async def scenario(mgr, client):
        ws = category.workspace("alpha")
        web_node = category.create_node(ws, {"label": "web"}, "user")
        doc = await _call(client, "POST", "/api/sessions/a/category/assign")
        assert doc["mode"] == "fork" and doc["fork"] == "a-cat1"
        assert doc["items"] == [{"kind": "session", "ref": "a"}, {"kind": "issue", "ref": "claunch-x1"}]
        fork = mgr.get("a-cat1")
        assert fork.sdef.quick_fork_of == "a"
        assert "# claunch category: ASSIGN TO TREE" in fork.sdef.task
        view = await _call(client, "GET", "/api/sessions/a/category")
        assert view["job"]["state"] == "running" and view["job"]["fork"] == "a-cat1"
        # one job at a time
        await _call(client, "POST", "/api/sessions/a/category/assign", status=409)
        # the fork assigns its origin's items
        out = await _call(client, "POST", "/api/category/agent-assign",
                          {"session": "a-cat1", "node_id": web_node["id"], "reason": "UI work"})
        assert out["path"] == ["web"] and "handoff" in out["next"]
        assert category.where(ws, "session", "a")["node"]["id"] == web_node["id"]
        assert category.where(ws, "issue", "claunch-x1")["node"]["id"] == web_node["id"]
        assert category.where(ws, "session", "a-cat1")["node"] is None
        # the merge is typed into the origin under this module's header
        merged = await _call(client, "POST", "/api/sessions/a-cat1/handoff",
                             {"text": "web: it builds the Category tab"})
        assert merged["target"] == "a"
        block = _typed_to(typed, "a")[-1]
        assert block.startswith("---\n# claunch category: ASSIGNED by a-cat1")
        assert "category: web\n" in block and block.endswith("web: it builds the Category tab")
        assert category.job(ws, "a")["state"] == category.DONE

    _run(scenario, tmp_path)


def test_a_fork_that_hands_in_without_assigning_fails_the_job(home, tmp_path, forkable, typed):
    async def scenario(mgr, client):
        ws = category.workspace("alpha")
        await _call(client, "POST", "/api/sessions/a/category/assign")
        await _call(client, "POST", "/api/sessions/a-cat1/handoff", {"text": "nothing fits"})
        assert _typed_to(typed, "a")[-1].startswith("---\n# claunch category: NOT ASSIGNED by a-cat1")
        assert category.job(ws, "a")["state"] == category.FAILED
        # a new request can start after a failed one
        doc = await _call(client, "POST", "/api/sessions/a/category/assign")
        assert doc["fork"] == "a-cat2"

    _run(scenario, tmp_path)


def test_a_fork_that_ended_without_merging_is_marked_failed(home, tmp_path, forkable, typed):
    async def scenario(mgr, client):
        ws = category.workspace("alpha")
        await _call(client, "POST", "/api/sessions/a/category/assign")
        category.job_update(ws, "a", category.RUNNING)
        mgr.get("a-cat1").exited = True
        view = await _call(client, "GET", "/api/sessions/a/category")
        assert view["job"]["state"] == "failed" and "ended without" in view["job"]["note"]

    _run(scenario, tmp_path)


def test_without_a_conversation_to_copy_the_job_runs_in_place(home, tmp_path, typed):
    async def scenario(mgr, client):
        ws = category.workspace("alpha")
        root = category.create_node(ws, {"label": "daemon"}, "user")
        view = await _call(client, "GET", "/api/sessions/a/category")
        assert view["mode"] == "inplace"
        doc = await _call(client, "POST", "/api/sessions/a/category/assign")
        assert doc["mode"] == "inplace" and doc["fork"] is None
        assert [s.sdef.name for s in mgr.list()] == ["a"]
        assert _typed_to(typed, "a")[-1].startswith("---\n# claunch category: ASSIGN TO TREE")
        out = await _call(client, "POST", "/api/category/agent-assign", {
            "session": "a", "reason": "restart gate work",
            "new": {"parent_id": root["id"], "label": "restart gate", "aliases": ["재기동 게이트"]}})
        assert out["proposed"] == "restart gate" and out["path"] == ["daemon"]
        assert category.job(ws, "a")["state"] == category.DONE
        (proposal,) = category.proposals(ws)
        assert {i["ref"] for i in proposal["items"]} == {"a", "claunch-x1"}
        assert category.where(ws, "session", "a")["node"]["id"] == root["id"]

    _run(scenario, tmp_path)


def test_an_in_place_request_never_acted_on_does_not_block_the_next(home, tmp_path, typed):
    async def scenario(mgr, client):
        ws = category.workspace("alpha")
        await _call(client, "POST", "/api/sessions/a/category/assign")
        # the session ignores the block; a second press replaces the job
        doc = await _call(client, "POST", "/api/sessions/a/category/assign")
        assert doc["mode"] == "inplace" and doc["job"]["state"] == "running"
        blocks = [b for b in _typed_to(typed, "a") if "# claunch category: ASSIGN TO TREE" in b]
        assert len(blocks) == 2
        # and a session that ends with the job still open fails it
        mgr.get("a").exited = True
        view = await _call(client, "GET", "/api/sessions/a/category")
        assert view["job"]["state"] == "failed" and "ended before assigning" in view["job"]["note"]
        assert category.job(ws, "a")["state"] == category.FAILED

    _run(scenario, tmp_path)


def test_a_held_session_is_woken_and_assigned(home, tmp_path, typed):
    async def scenario(mgr, client):
        session = mgr.get("a")
        session.exited, session.held_at = True, "2026-10-18T00:00:00"
        woken = []

        async def wake_async(name, *, by=""):
            woken.append((name, by))
            session.exited, session.held_at = False, None
            return session

        mgr.wake_async = wake_async
        view = await _call(client, "GET", "/api/sessions/a/category")
        assert view["life"] == "hold"
        doc = await _call(client, "POST", "/api/sessions/a/category/assign")
        assert woken == [("a", "category-assign")]
        assert doc["mode"] == "inplace" and doc["job"]["state"] == "running"
        assert _typed_to(typed, "a")[-1].startswith("---\n# claunch category: ASSIGN TO TREE")

    _run(scenario, tmp_path)


def test_a_killed_session_is_woken_only_when_asked(home, tmp_path, typed):
    async def scenario(mgr, client):
        ws = category.workspace("alpha")
        mgr.kill("a", force=True)
        for _ in range(100):
            if mgr.get("a").exited:
                break
            await asyncio.sleep(0.05)
        assert mgr.get("a").exited
        view = await _call(client, "GET", "/api/sessions/a/category")
        assert view["life"] == "killed"
        refused = await _call(client, "POST", "/api/sessions/a/category/assign", status=409)
        assert refused["needs_wake"] is True and refused["life"] == "killed"
        assert category.job(ws, "a") is None and mgr.get("a").exited
        doc = await _call(client, "POST", "/api/sessions/a/category/assign", {"wake": True})
        assert not mgr.get("a").exited
        assert doc["mode"] == "inplace" and doc["job"]["state"] == "running"
        assert _typed_to(typed, "a")[-1].startswith("---\n# claunch category: ASSIGN TO TREE")
        view = await _call(client, "GET", "/api/sessions/a/category")
        assert view["life"] == "running"

    _run(scenario, tmp_path)


def test_agent_assign_refusals_carry_the_owning_node(home, tmp_path, typed):
    async def scenario(mgr, client):
        ws = category.workspace("alpha")
        node = category.create_node(ws, {"label": "Web UI"}, "user")
        doc = await _call(client, "POST", "/api/category/agent-assign",
                          {"session": "a", "reason": "r", "new": {"label": "web-ui"}}, status=409)
        assert doc["candidates"][0]["id"] == node["id"]
        await _call(client, "POST", "/api/category/agent-assign", {"session": "a"}, status=400)
        await _call(client, "POST", "/api/category/agent-assign", {"session": "nope", "node_id": "x"}, status=404)
        await _call(client, "POST", "/api/category/agent-assign", {"node_id": "x"}, status=400)

    _run(scenario, tmp_path)


def test_unregistered_workspace_is_refused(home, tmp_path, typed):
    async def scenario(mgr, client):
        mgr.create(SessionDef(name="b", harness="py", cwd=str(tmp_path.parent)))
        await _call(client, "POST", "/api/sessions/b/category/assign", status=404)
        await _call(client, "GET", "/api/sessions/b/category", status=404)

    _run(scenario, tmp_path)


def test_tree_routes_edit_extract_merge_and_review(home, tmp_path):
    from types import SimpleNamespace

    workspaces.add(str(tmp_path), "alpha")
    sessions = {"s1": SimpleNamespace(sdef=SimpleNamespace(note="tab work", role="worker", issue="i-1"),
                                      status=lambda: "idle")}

    def get(name):
        if name not in sessions:
            raise KeyError(name)
        return sessions[name]

    async def scenario():
        app = web.Application()
        app["manager"] = SimpleNamespace(get=get)
        category_api.install(app)
        async with TestClient(TestServer(app)) as client:
            async def req(method, path, body=None, status=200):
                sep = "&" if "?" in path else "?"
                resp = await client.request(method, path + sep + "workspace=alpha", json=body)
                doc = await resp.json()
                assert resp.status == status, doc
                return doc

            web_ = (await req("POST", "/api/category/nodes", {"label": "web"}))["node"]
            tabs = (await req("POST", "/api/category/nodes", {"label": "tabs", "parent_id": web_["id"]}))["node"]
            await req("POST", "/api/category/nodes", {"label": "WEB"}, 409)
            await req("POST", "/api/category/assign", {"node_id": web_["id"], "items": [
                {"kind": "session", "ref": "s1"}, {"kind": "session", "ref": "gone"}]})
            tree = await req("GET", "/api/category")
            assert {tuple(n["path"]) for n in tree["nodes"]} == {("web",), ("web", "tabs")}
            assert tree["details"]["session:s1"] == {"status": "idle", "note": "tab work",
                                                     "role": "worker", "issue": "i-1"}
            assert tree["details"]["session:gone"] == {"status": "gone"}
            assert (await req("GET", "/api/category/search?q=tab"))["nodes"][0]["id"] == tabs["id"]
            out = await req("POST", "/api/category/extract", {
                "node_id": web_["id"], "items": [{"kind": "session", "ref": "s1"}], "label": "routing"})
            assert out["node"]["path"] == ["web", "routing"]
            items = await req("GET", f"/api/category/nodes/{web_['id']}/items")
            assert {i["ref"] for i in items["items"]} == {"s1", "gone"}
            merged = await req("POST", "/api/category/merge", {
                "nodes": [tabs["id"], out["node"]["id"]], "label": "navigation"})
            assert merged["node"]["parent_id"] == web_["id"]
            assert set(merged["node"]["aliases"]) == {"tabs", "routing"}
            p = category.propose(category.workspace("alpha"), {"label": "panes", "reason": "r"}, "agent")
            assert [x["id"] for x in (await req("GET", "/api/category/proposals"))["proposals"]] == [p["id"]]
            reviewed = await req("POST", f"/api/category/proposals/{p['id']}/review",
                                 {"decision": "accept", "label": "split panes"})
            assert reviewed["node"]["label"] == "split panes"
            await req("PUT", f"/api/category/nodes/{web_['id']}", {"parent_id": merged["node"]["id"]}, 400)
            await req("DELETE", f"/api/category/nodes/{web_['id']}", None, 409)
            await req("POST", "/api/category/unassign", {"items": [{"kind": "session", "ref": "gone"}]})
            assert (await req("GET", "/api/category/events"))["events"][0]["op"] == "unassign"
            assert (await client.get("/api/category?workspace=nope")).status == 404
            assert (await client.post("/api/category/nodes?workspace=alpha", json=[])).status == 400

    asyncio.run(scenario())


def test_tree_lists_the_workspace_sessions_not_yet_placed(home, tmp_path):
    from types import SimpleNamespace

    (tmp_path / "repo").mkdir()
    (tmp_path / "other").mkdir()
    workspaces.add(str(tmp_path / "repo"), "alpha")

    def sess(name, cwd, exited=False):
        return SimpleNamespace(sdef=SimpleNamespace(name=name, cwd=str(cwd), note="", role="", issue=""),
                               exited=exited, status=lambda: "idle")

    sessions = [sess("placed", tmp_path / "repo"), sess("loose", tmp_path / "repo"),
                sess("dead", tmp_path / "repo", exited=True), sess("elsewhere", tmp_path / "other")]
    by_name = {s.sdef.name: s for s in sessions}

    async def scenario():
        app = web.Application()
        app["manager"] = SimpleNamespace(get=lambda n: by_name[n], list=lambda: sessions)
        category_api.install(app)
        ws = category.workspace("alpha")
        node = category.create_node(ws, {"label": "web"}, "user")
        category.assign(ws, [{"kind": "session", "ref": "placed"}], node["id"], "user")
        async with TestClient(TestServer(app)) as client:
            doc = await (await client.get("/api/category?workspace=alpha")).json()
        assert doc["unassigned"] == [{"kind": "session", "ref": "loose"}]
        assert doc["details"]["session:loose"]["status"] == "idle"

    asyncio.run(scenario())
