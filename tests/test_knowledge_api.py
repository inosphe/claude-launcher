import asyncio

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
