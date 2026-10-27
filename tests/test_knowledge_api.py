import asyncio
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import knowledge, workspaces
from claude_launcher.daemon import knowledge_api


def test_http_crud_search_export_and_errors(home, tmp_path):
    ws = workspaces.add(str(tmp_path), "alpha")
    folder = knowledge.files_dir(ws)
    folder.mkdir(parents=True)
    (folder / "v.md").write_text("---\nkind: vision\ntitle: 파일 비전\n---\n방향\n", encoding="utf-8")

    async def scenario():
        app = web.Application()
        knowledge_api.install(app)
        async with TestClient(TestServer(app)) as client:
            async def request(method, path="", body=None, status=200, query=""):
                response = await client.request(method, "/api/knowledge" + path + "?workspace=alpha" + query,
                                                json=body)
                assert response.status == status, await response.text()
                return await response.json()
            row = (await request("POST", body={"kind": "decision", "title": "결정", "body": "이유",
                                               "author": "s9"}))["record"]
            assert row["author"] == "s9"
            listed = (await request("GET"))["records"]
            assert {r["id"] for r in listed} == {row["id"], "md:v"}
            assert [r["id"] for r in (await request("GET", query="&q=비전"))["records"]] == ["md:v"]
            assert (await request("GET", query="&kind=decision"))["records"][0]["id"] == row["id"]
            assert (await request("PUT", "/" + row["id"], {"title": "고친 결정"}))["record"]["title"] == "고친 결정"
            await request("PUT", "/md:v", {"title": "x"}, 409)
            await request("POST", body={"kind": "decision", "title": "", "body": "b"}, status=400)
            await request("POST", body=[], status=400)
            await request("GET", query="&limit=oops", status=400)
            await request("GET", query="&kind=nope", status=400)
            export = await client.get("/api/knowledge/export?workspace=alpha")
            assert export.status == 200 and export.content_type == "text/markdown"
            assert "### 고친 결정" in await export.text()
            await request("DELETE", "/" + row["id"])
            await request("DELETE", "/" + row["id"], status=404)
            assert (await client.get("/api/knowledge?workspace=missing")).status == 404
    asyncio.run(scenario())


def test_the_daemon_app_registers_the_knowledge_routes(home):
    from claude_launcher.daemon.api import build_app
    from claude_launcher.daemon.manager import SessionManager

    async def build():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        return build_app(mgr, "sekrit", started_at=time.monotonic())

    app = asyncio.run(build())
    routes = {(r.method, r.resource.canonical) for r in app.router.routes() if r.resource is not None}
    assert {("GET", "/api/knowledge"), ("POST", "/api/knowledge"), ("PUT", "/api/knowledge/{id}"),
            ("DELETE", "/api/knowledge/{id}"), ("GET", "/api/knowledge/{id}/history")} <= routes


def test_history_returns_the_versions_oldest_first(home, tmp_path):
    ws = workspaces.add(str(tmp_path), "alpha")
    folder = knowledge.files_dir(ws)
    folder.mkdir(parents=True)
    (folder / "v.md").write_text("---\nkind: vision\ntitle: 파일 비전\n---\n방향\n", encoding="utf-8")

    async def scenario():
        app = web.Application()
        knowledge_api.install(app)
        async with TestClient(TestServer(app)) as client:
            created = await client.post("/api/knowledge?workspace=alpha",
                                        json={"kind": "decision", "title": "결정",
                                              "body": "v1", "author": "s1"})
            row = (await created.json())["record"]
            updated = await client.put("/api/knowledge/" + row["id"] + "?workspace=alpha",
                                       json={"body": "v2", "author": "s2"})
            assert updated.status == 200
            response = await client.get("/api/knowledge/" + row["id"] + "/history?workspace=alpha")
            assert response.status == 200
            versions = (await response.json())["versions"]
            assert [(v["version"], v["body"], v["current"]) for v in versions] == [
                (1, "v1", False), (2, "v2", True)]
            assert versions[0]["archived_by"] == "s2"
            assert (await client.get("/api/knowledge/nope/history?workspace=alpha")).status == 404
            assert (await client.get("/api/knowledge/" + row["id"] +
                                     "/history?workspace=alpha&limit=oops")).status == 400
            file_history = await client.get("/api/knowledge/md%3Av/history?workspace=alpha")
            assert file_history.status == 200
            assert [(v["version"], v["current"]) for v in (await file_history.json())["versions"]] == [
                (1, True)]
    asyncio.run(scenario())


def test_a_listing_without_status_shows_active_records_only(home, tmp_path):
    ws = workspaces.add(str(tmp_path), "alpha")
    old = knowledge.create(ws, {"kind": "decision", "title": "옛 결정", "body": "이유"})
    new = knowledge.create(ws, {"kind": "decision", "title": "새 결정", "body": "이유",
                                "supersedes": old["id"]})

    async def scenario():
        app = web.Application()
        knowledge_api.install(app)
        async with TestClient(TestServer(app)) as client:
            async def ids(query=""):
                response = await client.get("/api/knowledge?workspace=alpha" + query)
                assert response.status == 200
                return {r["id"] for r in (await response.json())["records"]}
            assert await ids() == {new["id"]}
            assert await ids("&status=all") == {old["id"], new["id"]}
            assert await ids("&status=superseded") == {old["id"]}
    asyncio.run(scenario())
