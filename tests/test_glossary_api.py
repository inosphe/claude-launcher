import asyncio
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer
from claude_launcher import workspaces
from claude_launcher.daemon import glossary_api


def test_http_crud_suggestions_search_resolution_and_errors(home, tmp_path):
    workspaces.add(str(tmp_path), "alpha")
    async def scenario():
        app = web.Application()
        glossary_api.install(app)
        async with TestClient(TestServer(app)) as client:
            async def request(method, path="", body=None, status=200):
                response = await client.request(method, "/api/glossary" + path + "?workspace=alpha", json=body)
                assert response.status == status, await response.text()
                return await response.json()
            proposal = (await request("POST", "/suggestions", {"term": "RAG", "definition": "검색 증강 생성", "aliases": ["retrieval augmented generation"], "proposed_by": "s94"}))["suggestion"]
            assert (await request("GET"))["terms"] == []
            assert (await request("GET", "/suggestions"))["suggestions"][0]["status"] == "pending"
            row = (await request("POST", "/suggestions/" + proposal["id"] + "/review", {"decision": "accept"}))["term"]
            await request("POST", "/suggestions/" + proposal["id"] + "/review", {"decision": "accept"}, 409)
            assert (await request("POST", "/resolve", {"text": "Use [[term:RAG]]"}))["references"][0]["term"]["id"] == row["id"]
            assert (await request("PUT", "/" + row["id"], {"definition": "수정된 정의"}))["term"]["definition"] == "수정된 정의"
            await request("POST", body={"term": "rag", "definition": "duplicate"}, status=409)
            await request("POST", body=[], status=400)
            await request("POST", "/resolve", {"text": 42}, 400)
            await request("POST", "/suggestions/nope/review", {"decision": "reject"}, 404)
            await request("DELETE", "/" + row["id"])
            assert (await request("GET"))["terms"] == []
            assert (await client.get("/api/glossary?workspace=missing")).status == 404
            assert (await client.get("/api/glossary?workspace=alpha&limit=oops")).status == 400
            malformed = await client.post("/api/glossary?workspace=alpha", data="{", headers={"Content-Type": "application/json"})
            assert malformed.status == 400
            created = await request("POST", body={"term": "checkpoint", "definition": "저장한 학습 상태"})
            fuzzy = await client.get("/api/glossary?workspace=alpha&q=checkpiont")
            assert (await fuzzy.json())["terms"][0]["id"] == created["term"]["id"]
    asyncio.run(scenario())
