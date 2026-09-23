import asyncio
import json
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


def test_opening_task_is_its_own_source_and_outlives_the_registry(tmp_path):
    root = tmp_path / "repo"

    class Board:
        async def issues(self, root): return []
        async def br(self, root, args): return []

    sdef = SimpleNamespace(name="s1", cwd=str(root), task="ship the kanban needle",
                           issue=None, identity="worker w1", note="")
    live = [SimpleNamespace(sdef=sdef, created_at="2026-09-21T00:00:00+00:00")]

    async def resolve(cwd): return root

    service = SimpleNamespace(manager=SimpleNamespace(list=lambda: list(live)), board=Board(),
                              known_roots=lambda: [], resolve_root=resolve)
    obs = SimpleNamespace(load_session=lambda n: {}, reports=SimpleNamespace(rows=lambda n: []))
    corpus = search_anything.Corpus(service, obs)

    docs = asyncio.run(corpus.docs())
    task_docs = [d for d in docs if d.meta["kind"] == "opening-task"]
    assert len(task_docs) == 1
    assert "ship the kanban needle" in task_docs[0].chunks[0]
    # A result the reader can open: the record endpoint returns the whole task.
    assert task_docs[0].meta["source_url"] == "api/search/records/s1/opening-task"
    assert task_docs[0].meta["href"] == "#/s/s1"
    # One match, one result: the session's own document no longer repeats it.
    session_doc = next(d for d in docs if d.meta["kind"] == "session")
    assert "ship the kanban needle" not in session_doc.chunks[0]
    assert "worker w1" in session_doc.chunks[0]

    # The registry entry is what a clear removes; the opening task is not.
    live.clear()
    docs = asyncio.run(corpus.docs())
    assert not [d for d in docs if d.meta["kind"] == "session"]
    survivors = [d for d in docs if d.meta["kind"] == "opening-task"]
    assert len(survivors) == 1 and "ship the kanban needle" in survivors[0].chunks[0]


def test_opening_task_record_replaces_itself_and_skips_empty_tasks():
    search_records.capture_task("s2", "first task", "2026-09-21T00:00:00+00:00")
    search_records.capture_task("s2", "second task", "2026-09-21T01:00:00+00:00")
    rows = [r for r in search_records.rows("s2") if r["kind"] == "opening-task"]
    assert len(rows) == 1 and rows[0]["text"] == "second task"
    assert search_records.capture_task("s3", "   ") is None
    assert search_records.capture_task("s3", None) is None
    assert search_records.rows("s3") == []


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


def test_search_answer_reports_the_sessions_state_read_now(tmp_path):
    """The state a result shows is the fleet's state when it was asked for.

    The index is embedded in the background, so a status stored in a
    document's metadata is only as old as the sync that wrote it — and the
    session a person is looking for may have gone busy or been paused since.
    This pins the two places a row can name a session (the row itself, and the
    chips a record carries) against a registry that moved after the index was
    written, and pins that reading it again cost no embedding.
    """

    class Sdef:
        def __init__(self, name):
            self.name = name

    class Sess:
        def __init__(self, name, status="idle", paused_at=None, archived_at=None):
            self.sdef = Sdef(name)
            self._status, self.paused_at, self.archived_at = status, paused_at, archived_at

        def status(self):
            return self._status

    sessions = [Sess("s1", "busy"), Sess("s9", "exited", paused_at="2026-09-20")]
    manager = SimpleNamespace(list=lambda: sessions)

    async def run():
        endpoint = Endpoint()
        server = await _start_endpoint(endpoint)
        cfg = _cfg(server, watch_interval=0)
        service = rag.RagService(manager=manager, config=lambda: cfg, root_dir=tmp_path / "index")

        async def docs():
            return (search_anything.documents("session:s1", "s1", "relay needle", kind="session",
                                              name="s1", sessions=[{"name": "s1"}], href="#/s/s1")
                    + search_anything.documents("beads:r:x-1", "x-1 · board", "relay needle",
                                                kind="beads", sessions=[{"name": "s1"}, {"name": "s9"},
                                                                        {"name": "s7"}]))

        service.all_docs = docs
        try:
            result = await service.search("all", "relay", wait=10, rerank=False)
            session_row = next(r for r in result["results"] if r["kind"] == "session")
            assert session_row["name"] == "s1"
            assert (session_row["status"], session_row["paused"], session_row["archived"]) == ("busy", False, False)
            assert session_row["sessions"][0]["status"] == "busy"
            record = next(r for r in result["results"] if r["kind"] == "beads")
            chips = {chip["name"]: chip for chip in record["sessions"]}
            assert chips["s1"]["status"] == "busy" and chips["s1"]["paused"] is False
            assert chips["s9"]["status"] == "exited" and chips["s9"]["paused"] is True
            # A name the registry does not hold stays exactly as it was
            # indexed: there is no live state to put beside it.
            assert chips["s7"] == {"name": "s7"}
            # The registry moves on; the next answer says so, and costs no
            # endpoint call at all — a state read from the registry is not a
            # reason to re-embed the corpus, and the repeated query answers
            # from the query-vector cache.
            embedded = len(endpoint.embed_calls)
            sessions[0]._status = "idle"
            again = await service.search("all", "relay", wait=10, rerank=False)
            assert next(r for r in again["results"] if r["kind"] == "session")["status"] == "idle"
            assert len(endpoint.embed_calls) == embedded
            assert again["timing"]["embed_cached"] is True
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


def test_rerank_candidates_are_cut_to_rerank_chars(tmp_path):
    """A candidate reaches the reranker as its title and RERANK_CHARS of body.

    The unified corpus keeps a whole chunk as a result's excerpt so the page
    can show any part of a long issue, and that excerpt is what the reranker
    text is built from. Uncut, a 30-result screen sent five times the
    reranker's intended budget per document; it was measured at 18.6 s of a
    29.7 s search.
    """

    async def run():
        endpoint = Endpoint()
        server = await _start_endpoint(endpoint)
        cfg = _cfg(server, watch_interval=0, rerank_top=2)
        service = rag.RagService(config=lambda: cfg, root_dir=tmp_path / "index")

        async def docs():
            body = "relay " + ("padding " * 900)
            return (search_anything.documents("beads:r:x-1", "x-1 long issue", body, kind="beads")
                    + search_anything.documents("beads:r:x-2", "x-2 long issue", body, kind="beads"))

        service.all_docs = docs
        try:
            result = await service.search("all", "relay", wait=10)
            assert result["reranked"] is True
            sent = endpoint.rerank_calls[-1]["documents"]
            assert len(sent) == 2
            for text in sent:
                title, _, excerpt = text.partition(chr(10))
                assert len(excerpt) <= rag.RERANK_CHARS
                assert title.startswith("x-")
            # The result itself still carries the whole chunk it was indexed
            # from: cutting is what the reranker reads, not what the page shows.
            assert len(result["results"][0]["excerpt"]) > rag.RERANK_CHARS
        finally:
            await service.shutdown()
            await server.close()

    asyncio.run(run())


def _stream_corpus(service):
    """Three records in the unified corpus: enough for the reranker to reorder."""
    async def docs():
        out = []
        out += search_anything.documents("a", "zebra notes", "relay stripes", kind="checks")
        out += search_anything.documents("b", "relay board", "relay relay relay", kind="checks")
        out += search_anything.documents("c", "other record", "relay once", kind="checks")
        return out
    service.all_docs = docs


def test_search_stream_answers_ranked_before_the_reranker_returns(tmp_path):
    """The first answer does not wait on the reranker.

    The reranker is the slow half of a search: 1.2-11 s on the live endpoint
    against 43-104 ms for the vector ranking (claunch-1sszr). This holds the
    reranker until the ranked answer has been read, so the test fails if the
    stream ever waits for it before yielding.
    """
    async def run():
        endpoint = Endpoint()
        server = await _start_endpoint(endpoint)
        cfg = _cfg(server)
        service = rag.RagService(config=lambda: cfg, root_dir=tmp_path / "index")
        _stream_corpus(service)
        gate = asyncio.Event()

        class HeldRerank(rag.RagClient):
            async def rerank(self, *args):
                await gate.wait()
                return await super().rerank(*args)

        service._client_factory = HeldRerank
        try:
            await service.search("all", "relay", wait=10, rerank=False)  # build the index
            embeds = len(endpoint.embed_calls)
            stream = service.search_stream("all", "relay", wait=10)
            event, ranked = await asyncio.wait_for(stream.__anext__(), 5)
            assert event == "ranked"
            assert ranked["results"] and ranked["rerank_pending"] is True
            assert ranked["reranked"] is False and ranked["timing"]["rerank_ms"] is None
            assert not any("rerank_score" in row for row in ranked["results"])
            assert not endpoint.rerank_calls
            gate.set()
            event, reranked = await asyncio.wait_for(stream.__anext__(), 5)
            assert event == "reranked"
            assert reranked["reranked"] is True and reranked["rerank_pending"] is False
            assert reranked["timing"]["rerank_ms"] is not None
            assert {r["id"] for r in reranked["results"]} == {r["id"] for r in ranked["results"]}
            with pytest.raises(StopAsyncIteration):
                await stream.__anext__()
            # One query embedding serves both answers.
            assert len(endpoint.embed_calls) <= embeds + 1
        finally:
            await service.shutdown()
            await server.close()
    asyncio.run(run())


def test_search_stream_keeps_the_ranked_answer_when_the_reranker_fails(tmp_path):
    """A reranker failure after ``ranked`` went out is an ``error`` event that
    carries the same results, so a page keeps what it is showing; ``search()``
    keeps its old contract on top of the stream (fall back for ``all``)."""
    async def run():
        endpoint = Endpoint()
        server = await _start_endpoint(endpoint)
        cfg = _cfg(server)
        service = rag.RagService(config=lambda: cfg, root_dir=tmp_path / "index")
        _stream_corpus(service)

        class FailedRerank(rag.RagClient):
            async def rerank(self, *args):
                raise rag.RagError("provider rerank unavailable")

        try:
            await service.search("all", "relay", wait=10, rerank=False)
            service._client_factory = FailedRerank
            events = [e async for e in service.search_stream("all", "relay", wait=10)]
            assert [name for name, _ in events] == ["ranked", "error"]
            ranked, failed = events[0][1], events[1][1]
            assert failed["results"] == ranked["results"]
            assert failed["error"] == "provider rerank unavailable"
            assert failed["warnings"] and failed["rerank_pending"] is False
            fallback = await service.search("all", "relay", wait=10)
            assert "error" not in fallback and fallback["warnings"] and not fallback["reranked"]
        finally:
            await service.shutdown()
            await server.close()
    asyncio.run(run())


def test_search_without_a_reranker_streams_one_event(tmp_path):
    """No reranker configured: ``ranked`` says nothing else is coming and the
    stream ends there."""
    async def run():
        endpoint = Endpoint()
        server = await _start_endpoint(endpoint)
        cfg = _cfg(server, rerank_model="")
        service = rag.RagService(config=lambda: cfg, root_dir=tmp_path / "index")
        _stream_corpus(service)
        try:
            events = [e async for e in service.search_stream("all", "relay", wait=10)]
            assert [name for name, _ in events] == ["ranked"]
            assert events[0][1]["rerank_pending"] is False
            assert not endpoint.rerank_calls
        finally:
            await service.shutdown()
            await server.close()
    asyncio.run(run())


def _read_sse(text):
    """``[(event, data)]`` from a server-sent event body."""
    out = []
    for block in text.replace("\r\n", "\n").split("\n\n"):
        fields = dict(line.split(": ", 1) for line in block.splitlines() if ": " in line)
        if "event" in fields:
            out.append((fields["event"], json.loads(fields["data"])))
    return out


def test_search_stream_route_speaks_server_sent_events(tmp_path):
    """``GET /api/search/stream``: two events in order on one response; a
    request that fails validation is plain JSON with the status the JSON
    route uses, before any stream starts."""
    from claude_launcher.daemon import api

    async def run():
        endpoint = Endpoint()
        server = await _start_endpoint(endpoint)
        cfg = _cfg(server, watch_interval=0)
        store.save({"rag": cfg})
        service = rag.RagService(root_dir=tmp_path / "index")
        _stream_corpus(service)
        app = web.Application()
        app["rag"] = service
        app.router.add_get("/api/search/stream", api.h_search_stream)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.get("/api/search/stream",
                                        params={"q": "relay", "kind": "all", "wait": "10"})
            assert response.status == 200
            assert response.headers["Content-Type"].startswith("text/event-stream")
            events = _read_sse(await response.text())
            assert [name for name, _ in events] == ["ranked", "reranked"]
            assert events[0][1]["rerank_pending"] is True
            assert events[1][1]["reranked"] is True
            response = await client.get("/api/search/stream", params={"q": "", "kind": "all"})
            assert response.status == 400 and (await response.json())["error"] == "q is required"
            response = await client.get("/api/search/stream", params={"q": "x", "kind": "nope"})
            assert response.status == 400
        finally:
            await service.shutdown()
            await client.close()
            await server.close()
    asyncio.run(run())
