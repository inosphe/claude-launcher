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


def test_composer_uses_target_session_workspace_and_returns_definition_snapshot(home, tmp_path):
    from types import SimpleNamespace
    from claude_launcher import glossary
    alpha = tmp_path / "alpha"
    beta = tmp_path / "beta"
    alpha.mkdir(); beta.mkdir()
    a = workspaces.add(str(alpha), "alpha")
    b = workspaces.add(str(beta), "beta")
    glossary.create(a, {"term": "자세히", "definition": "구체적인 설명"})
    glossary.create(b, {"term": "자세히", "definition": "추가 탐색"})
    sessions = {name: SimpleNamespace(sdef=SimpleNamespace(cwd=str(path)))
                for name, path in [("s1", alpha), ("s2", beta), ("s3", tmp_path / "unregistered")]}
    async def scenario():
        app = web.Application()
        app["manager"] = SimpleNamespace(get=lambda name: sessions[name])
        glossary_api.install(app)
        async with TestClient(TestServer(app)) as client:
            for name, meaning in [("s1", "구체적인 설명"), ("s2", "추가 탐색")]:
                response = await client.get(f"/api/sessions/{name}/glossary?q=자세히&workspace=beta")
                data = await response.json()
                assert data["terms"][0]["definition"] == meaning
                response = await client.post(f"/api/sessions/{name}/glossary", json={
                    "text": "[[term:자세히]] 해줘", "action": "compose", "workspace": "ignored"})
                data = await response.json()
                assert data["ready"] and meaning in data["delivery"]
            assert (await client.post("/api/sessions/s1/glossary", json=[])).status == 400
            assert (await client.post("/api/sessions/s1/glossary", json={"action": "oops"})).status == 400
            assert (await client.get("/api/sessions/s3/glossary?q=자세히")).status == 404
    asyncio.run(scenario())
