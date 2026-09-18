import asyncio
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import observer, rag, search_anything, search_records, status_checks
from test_rag import Endpoint, _start_endpoint, _cfg


def test_archive_survives_display_retention_and_updates():
    events = [{"id": str(i), "kind": "test", "text": f"test {i}", "at": str(i)} for i in range(210)]
    search_records.remember("s1", events)
    search_records.remember("s1", events[-200:])
    search_records.remember("s1", [{**events[0], "acknowledged": True}])
    assert len(search_records.rows("s1")) == 210
    assert search_records.find("s1", "0")["acknowledged"] is True
    assert search_records.find("s2", "0") is None


def test_checks_events_preserve_old_answers_without_poll_duplicates():
    status_checks.set_entries([{"id": "c1", "name": "tests", "question": "Tests pass?"}])
    status_checks.report("s1", [{"id": "c1", "answer": "no"}])
    status_checks.report("s1", [{"id": "c1", "answer": "yes"}])
    session = SimpleNamespace(sdef=SimpleNamespace(name="s1", cwd="/repo"), exited=False, info=lambda: {})
    service = observer.Observer(SimpleNamespace(list=lambda: [session]), SimpleNamespace(meshes_for_session=lambda n: []))
    first = service.snapshot()["sessions"][0]["events"]
    second = service.snapshot()["sessions"][0]["events"]
    assert first == second and len(first) == 2
    assert [search_records.find("s1", e["id"])["evidence"][0]["answer"] for e in first] == ["no", "yes"]


def test_config_preserves_secret_and_removes_requested_dimensions():
    store.save({"rag": {"base_url": "http://localhost:123/v1", "api_key": "secret", "embedding_model": "embed", "dimensions": 1024}})
    doc = search_anything.proposed({"api_key": "", "rerank_model": "rerank"})
    assert doc["rag"]["api_key"] == "secret"
    assert "dimensions" not in doc["rag"]
    assert "secret" not in str(search_anything.public_settings())
    for bad in ({"dimensions": 123}, {"batch": 1.5}, {"timeout": float("nan")}, {"base_url": "http://user:pass@host/v1"}):
        with pytest.raises(ValueError):
            search_anything.proposed(bad)


def test_unified_corpus_includes_comments_events_and_session_links(tmp_path):
    root = tmp_path / "repo"
    issue = {"id": "x-1", "title": "board", "description": "body", "assignee": "s1",
             "comments": [{"id": 1, "text": "relay comment needle"}]}
    class Board:
        async def issues(self, root): return [issue]
        async def br(self, root, args): return [issue]
    session = SimpleNamespace(sdef=SimpleNamespace(name="s1", cwd=str(root), task="task", issue="x-1"))
    async def resolve(cwd): return root
    service = SimpleNamespace(manager=SimpleNamespace(list=lambda: [session]), board=Board(),
                              known_roots=lambda: [root], resolve_root=resolve)
    obs = SimpleNamespace(load_session=lambda n: {}, reports=SimpleNamespace(rows=lambda n: []))
    search_records.capture("s1", "briefing", {"goal": "kanban needle"}, "2026-09-18")
    docs = asyncio.run(search_anything.Corpus(service, obs).docs())
    comment = next(d for d in docs if d.meta["kind"] == "comment")
    assert "relay comment needle" in comment.chunks[0]
    assert comment.meta["sessions"] == [{"name": "s1", "via": ["link", "assignee"]}]
    assert any(d.meta["kind"] == "briefing" and "kanban needle" in d.chunks[0] for d in docs)
    # Content beyond the old eight-chunk limit must remain searchable.
    long = search_anything.documents("long", "title", "x" * 30000 + "tail needle")
    assert "tail needle" in long[-1].chunks[0]


def test_settings_routes_test_without_saving_and_persist_models(tmp_path):
    async def run():
        endpoint = Endpoint()
        server = await _start_endpoint(endpoint)
        cfg = _cfg(server, watch_interval=0)
        store.save({"rag": cfg})
        service = rag.RagService(root_dir=tmp_path / "index")
        app = web.Application()
        app["rag"] = service
        app["observer"] = SimpleNamespace()
        search_anything.install(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/rag/test", json={"embedding_model": "new", "api_key": ""})
            assert response.status == 200
            assert (await response.json())["dimensions"] == 8
            assert store.rag_config()["embedding_model"] == "emb"
            assert "dimensions" not in endpoint.embed_calls[-1]
            assert len(endpoint.rerank_calls) == 1
            response = await client.put("/api/rag/settings", json={"embedding_model": "new", "api_key": ""})
            assert response.status == 200
            assert "rag-secret" not in await response.text()
            assert store.rag_config()["embedding_model"] == "new"
            assert store.rag_config()["api_key"] == "rag-secret"
            class FailedRerank(rag.RagClient):
                async def rerank(self, *args):
                    raise rag.RagError("private provider error")
            service._client_factory = FailedRerank
            response = await client.post("/api/rag/test", json={})
            failure = await response.json()
            assert response.status == 400 and failure["stage"] == "rerank" and failure["dimensions"] == 8
            assert "private provider error" not in str(failure)
            service._client_factory = rag.RagClient
            endpoint.fail_embed = True
            response = await client.post("/api/rag/test", json={})
            assert response.status == 400 and "rag-secret" not in await response.text()
        finally:
            await service.shutdown()
            await client.close()
            await server.close()
    asyncio.run(run())


def test_all_search_uses_reranker_and_rebuilds_for_endpoint_change(tmp_path):
    async def run():
        endpoint = Endpoint()
        server = await _start_endpoint(endpoint)
        cfg = _cfg(server)
        service = rag.RagService(config=lambda: cfg, root_dir=tmp_path / "index")
        async def docs():
            return search_anything.documents("a", "record", "relay needle", kind="checks", sessions=[{"name": "s1"}])
        service.all_docs = docs
        try:
            result = await service.search("all", "relay", wait=10)
            assert result["results"][0]["kind"] == "checks"
            assert result["reranked"] and endpoint.rerank_calls
            class FailedRerank(rag.RagClient):
                async def rerank(self, *args):
                    raise rag.RagError("provider rerank unavailable")
            service._client_factory = FailedRerank
            fallback = await service.search("all", "relay", wait=10)
            assert fallback["results"] and not fallback["reranked"]
            assert fallback["warnings"]
            index = service._index("all", None, cfg)
            assert index.dims == 8 and index.entries
            cfg["base_url"] += "/changed"
            assert not service._index("all", None, cfg).entries
        finally:
            await service.shutdown()
            await server.close()
    asyncio.run(run())


def test_board_and_record_changes_queue_unified_search(tmp_path):
    service = rag.RagService()
    service.all_docs = object()
    queued = []
    service.enqueue = lambda kind, root=None: queued.append(kind)
    service.on_board_write(tmp_path)
    assert queued == ["beads", "all"]
    queued.clear()
    service._board_moved(tmp_path)
    assert queued == ["beads", "all"]
    queued.clear()
    hook = lambda: queued.append("all")
    search_records.change_hooks.append(hook)
    try:
        search_records.capture("s1", "checks", {"answer": "yes"}, "2026-09-18")
        search_records.capture("s1", "checks", {"answer": "yes"}, "2026-09-18")
        assert queued == ["all"]
    finally:
        search_records.change_hooks.remove(hook)
