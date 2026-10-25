import asyncio
import json
import threading
from types import SimpleNamespace

import pytest
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import observer, rag, search_anything, search_records, status_checks
from test_rag import Endpoint, _start_endpoint, _cfg


def _world(tmp_path, n_sessions=3):
    """A registry of sessions with observer events, reports, daemon events
    and opening tasks, on one board with a commented issue."""
    root = tmp_path / "repo"
    issue = {"id": "x-1", "title": "board", "description": "body",
             "comments": [{"id": 1, "text": "comment needle"}]}

    class Board:
        async def issues(self, root): return [issue]
        async def br(self, root, args): return [issue]

    sessions = [SimpleNamespace(sdef=SimpleNamespace(name=f"s{i}", cwd=str(root), task=f"task needle {i}",
                                                     issue=None, identity=f"w{i}", note=""),
                                created_at="2026-09-23T00:00:00+00:00")
                for i in range(n_sessions)]

    async def resolve(cwd): return root

    service = SimpleNamespace(manager=SimpleNamespace(list=lambda: list(sessions)), board=Board(),
                              known_roots=lambda: [root], resolve_root=resolve)
    obs = SimpleNamespace(
        load_session=lambda n: {"events": [{"id": "o1", "kind": "observer", "text": "observed " + n, "at": "1"}],
                                "summary": "summary " + n},
        reports=SimpleNamespace(rows=lambda n: [{"id": "r1", "kind": "report", "text": "report " + n, "at": "2"}]),
        session_events=SimpleNamespace(rows=lambda s: [{"id": "e1", "kind": "action",
                                                        "text": "acted " + s.sdef.name, "at": "3"}]))
    return search_anything.Corpus(service, obs), sessions


def test_the_corpus_is_built_off_the_event_loop(tmp_path, monkeypatch):
    """The event loop is the one every terminal's keystrokes wait on, and a
    pass hashes every chunk of every issue, comment and record and writes
    every session's records: 17 s on this machine, and 56% of the loop's busy
    time in a profile taken during one (claunch-lol7h). The hashing and the
    writes happen on a worker thread."""
    corpus, _ = _world(tmp_path)
    seen = {"hash": set(), "write": set()}
    real_hash, real_write = rag.content_hash, search_records.remember_many

    def spy_hash(*parts):
        seen["hash"].add(threading.get_ident())
        return real_hash(*parts)

    def spy_write(batch, **kw):
        seen["write"].add(threading.get_ident())
        return real_write(batch, **kw)

    monkeypatch.setattr(rag, "content_hash", spy_hash)
    monkeypatch.setattr(search_records, "remember_many", spy_write)

    async def run():
        loop_thread = threading.get_ident()
        docs = await corpus.docs()
        return loop_thread, docs

    loop_thread, docs = asyncio.run(run())
    assert docs
    assert seen["hash"] and loop_thread not in seen["hash"]
    assert seen["write"] and loop_thread not in seen["write"]


def test_a_pass_does_not_queue_another_pass_for_its_own_writes(tmp_path):
    """The records a pass writes are read back later in the same pass, so
    they are already in what it returns. Firing the change hook for them --
    whose production handler is enqueue("all") -- queued a second full pass
    that found nothing new: 26 hook calls on a twelve-session fixture before
    this change, 0 after, with identical documents."""
    corpus, _ = _world(tmp_path)
    fired = []
    hook = lambda: fired.append(1)
    search_records.change_hooks.append(hook)
    try:
        docs = asyncio.run(corpus.docs())
    finally:
        search_records.change_hooks.remove(hook)
    assert fired == []
    texts = [d.chunks[0] for d in docs]
    # ...and what it wrote is in what it returned.
    for needle in ("observed s1", "report s1", "acted s1", "task needle 1"):
        assert any(needle in t for t in texts), needle


def test_another_writer_still_fires_the_change_hook():
    """Only the corpus's own writes are quiet. An observer save or a report
    elsewhere is news the index has not seen, and must queue a pass."""
    fired = []
    hook = lambda: fired.append(1)
    search_records.change_hooks.append(hook)
    try:
        search_records.remember("s9", [{"id": "a", "text": "new", "at": "1"}])
        assert fired == [1]
        search_records.remember("s9", [{"id": "a", "text": "new", "at": "1"}])
        assert fired == [1], "an unchanged row is not a change"
    finally:
        search_records.change_hooks.remove(hook)


def test_one_connection_for_every_session_in_a_pass(tmp_path, monkeypatch):
    """Three writes per registered session, each opening the database, was
    hundreds of connections per pass on this machine."""
    corpus, _ = _world(tmp_path, n_sessions=20)
    opened = []
    real = search_records.database

    def counting():
        opened.append(1)
        return real()

    monkeypatch.setattr(search_records, "database", counting)
    asyncio.run(corpus.docs())
    # One for the batched writes, one for reading the records back; the
    # one-time import of pre-upgrade snapshots reads once more.
    assert len(opened) <= 3, len(opened)


def test_a_batch_applies_in_order_like_separate_calls():
    """The batch replaces calls made one after another, so a later entry for
    the same record wins, as the later call did."""
    search_records.remember_many([
        ("s1", [{"id": "k", "text": "first", "at": "1"}]),
        ("s1", [{"id": "k", "text": "second", "at": "2"}]),
        ("s2", []),
    ], notify=False)
    assert search_records.find("s1", "k")["text"] == "second"
    assert search_records.rows("s2") == []
    assert search_records.remember_many([], notify=False) is False


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
    # The per-path values are stored in their row, and the first save after a
    # legacy block writes the rows and drops the flat copies (claunch-pxxn4).
    assert doc["rag"]["sources"]["custom"]["api_key"] == "secret"
    assert "api_key" not in doc["rag"] and "base_url" not in doc["rag"]
    assert "dimensions" not in doc["rag"]
    assert "secret" not in str(search_anything.public_settings())
    for bad in ({"dimensions": 123}, {"batch": 1.5}, {"timeout": float("nan")}, {"base_url": "http://user:pass@host/v1"}):
        with pytest.raises(ValueError):
            search_anything.proposed(bad)


def test_unified_corpus_includes_comments_events_and_session_links(tmp_path):
    root = tmp_path / "repo"
    issue = {"id": "x-1", "title": "board", "description": "body", "assignee": "s1", "created_by": "s2",
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
    for doc in docs:
        if doc.meta["kind"] in ("beads", "comment"):
            assert doc.meta["assignee"] == "s1"
            assert doc.meta["created_by"] == "s2"
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
            index = await service._index("all", None, cfg)
            assert index.dims == 8 and index.entries
            cfg["base_url"] += "/changed"
            assert not (await service._index("all", None, cfg)).entries
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


def test_body_keyword_rescues_a_document_outside_the_vector_candidates(tmp_path):
    async def run():
        endpoint = Endpoint()
        server = await _start_endpoint(endpoint)
        service = rag.RagService(config=lambda: _cfg(server, candidates=3, rerank_model=""),
                                 root_dir=tmp_path / "index")

        async def docs():
            out = search_anything.documents("target", "pytest 프로필 지침",
                "`.beads/issues.jsonl`을 직접 수정 금지", kind="beads", root="A", issue="target")
            for i in range(8):
                out += search_anything.documents(f"noise{i}", "beads unrelated",
                    "ordinary workflow", kind="checks")
            return out

        service.all_docs = docs
        try:
            view = await service.search("all", "issues.jsonl", limit=3, wait=10)
            assert view["results"][0]["id"] == "target:0"
            assert view["results"][0]["lexical"] is True
            assert "직접 수정 금지" in view["results"][0]["excerpt"]
            # The keyword postings are reconstructed from durable metadata.
            await service.shutdown()
            service = rag.RagService(config=lambda: _cfg(server, candidates=3, rerank_model=""),
                                     root_dir=tmp_path / "index")
            service.all_docs = docs
            again = await service.search("all", "issues.jsonl", limit=3, wait=10)
            assert again["results"][0]["id"] == "target:0"
        finally:
            await service.shutdown()
            await server.close()
    asyncio.run(run())


def test_grouped_search_has_diverse_candidates_before_and_after_reranking(tmp_path):
    async def run():
        endpoint = Endpoint()
        server = await _start_endpoint(endpoint)
        service = rag.RagService(config=lambda: _cfg(server, candidates=3, rerank_top=3),
                                 root_dir=tmp_path / "index")

        async def docs():
            out = []
            for i in range(20):
                out += search_anything.documents(f"comment:A:x:{i}", "relay issue",
                    "relay " * (i + 1), kind="comment", root="A", issue="x",
                    source_url="api/beads/x?cwd=A")
            out += search_anything.documents("beads:B:x", "relay issue",
                "relay another board", kind="beads", root="B", issue="x")
            out += search_anything.documents("beads:A:y", "relay issue",
                "relay other issue", kind="beads", root="A", issue="y")
            return out

        service.all_docs = docs
        try:
            events = [v async for v in service.search_stream("all", "relay", limit=3, wait=10)]
            assert [event for event, _ in events] == ["ranked", "reranked"]
            for _, view in events:
                rows = view["results"]
                assert len(rows) == 3
                assert {(r["root"], r["issue"]) for r in rows} == {("A", "x"), ("B", "x"), ("A", "y")}
                grouped = next(r for r in rows if (r["root"], r["issue"]) == ("A", "x"))
                assert len(grouped["matches"]) == 2
                assert grouped["source_url"] == "api/beads/x?cwd=A"
            assert len(endpoint.rerank_calls[-1]["documents"]) == 3
            assert all(len(t.partition("\n")[2]) <= rag.RERANK_CHARS
                       for t in endpoint.rerank_calls[-1]["documents"])
        finally:
            await service.shutdown()
            await server.close()
    asyncio.run(run())


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


def test_active_search_filters_before_limits_and_refreshes_liveness(tmp_path):
    async def run():
        endpoint = Endpoint()
        server = await _start_endpoint(endpoint)
        cfg = _cfg(server, candidates=2, rerank_top=2, watch_interval=0)
        live = SimpleNamespace(sdef=SimpleNamespace(name="live"), exited=False)
        sessions = [live, SimpleNamespace(sdef=SimpleNamespace(name="dead"), exited=True),
                    SimpleNamespace(sdef=SimpleNamespace(name="paused"), exited=True, paused_at="now"),
                    SimpleNamespace(sdef=SimpleNamespace(name="archived"), exited=False, archived_at="now")]
        service = rag.RagService(config=lambda: cfg, root_dir=tmp_path / "index",
                                 manager=SimpleNamespace(list=lambda: sessions))

        async def docs():
            out = []
            # Many irrelevant exact and vector matches must not exhaust the
            # candidate window before the live task and owned board record.
            for i in range(30):
                out += search_anything.documents(f"dead{i}", "relay", "relay", kind="checks", sessions=[{"name": "dead"}])
            for name in ("paused", "archived", "removed"):
                out += search_anything.documents(name, "relay", "relay", kind="opening-task", sessions=[{"name": name}])
            out += search_anything.documents("live-task", "relay task", "relay task details", kind="opening-task", sessions=[{"name": "live"}])
            for name in ("dead", "paused", "archived", "removed", None):
                for kind in ("beads", "comment"):
                    out += search_anything.documents(f"irrelevant-{kind}-{name}", "relay", "relay details",
                                                     kind=kind, assignee=name, created_by=name,
                                                     sessions=[{"name": "live", "via": ["link", "task"]}])
            out += search_anything.documents("board", "relay board", "relay closed issue", kind="beads",
                                             assignee="live", created_by="dead", sessions=[])
            out += search_anything.documents("comment", "relay comment", "relay comment details", kind="comment",
                                             assignee="dead", created_by="live", sessions=[])
            return out
        service.all_docs = docs
        try:
            general = await service.search("all", "relay", limit=50, wait=10, rerank=False)
            assert any(r["id"].startswith("dead") for r in general["results"])
            active = await service.search("all", "relay", limit=3, mode="active", wait=10)
            assert {r["id"] for r in active["results"]} == {"live-task:0", "board:0", "comment:0"}
            assert all("dead" not in text for text in endpoint.rerank_calls[-1]["documents"])
            partial = await service.search("all", "relay unmatched-title-word", limit=2, mode="active", wait=0)
            assert len(partial["results"]) == 2
            # BM25 admits the matching term even when another query term is absent.
            assert all(r["lexical"] and r["id"] in {"live-task:0", "board:0", "comment:0"}
                       for r in partial["results"])
            stream = service.search_stream("all", "relay", limit=3, mode="active", wait=0)
            _, first = await stream.__anext__()
            assert any(r["id"] == "live-task:0" for r in first["results"])
            live.exited = True
            _, reranked = await stream.__anext__()
            assert reranked["results"] == []
            await stream.aclose()
            # Reusing the same index must reflect process exit without a sync.
            active = await service.search("all", "relay", limit=3, mode="active", wait=0)
            assert active["results"] == []
            live.exited = False
            live.archived_at = "now"
            active = await service.search("all", "relay", mode="active", wait=0)
            assert active["results"] == []
            live.archived_at = None
            restored = await service.search("all", "relay", mode="active", wait=0)
            assert {r["id"] for r in restored["results"]} == {"live-task:0", "board:0", "comment:0"}
            service.manager = None
            active = await service.search("all", "relay", mode="active", wait=0)
            assert active["results"] == []
        finally:
            await service.shutdown()
            await server.close()
    asyncio.run(run())


def test_active_beads_ownership_changes_without_text_changes(tmp_path):
    async def run():
        endpoint = Endpoint()
        server = await _start_endpoint(endpoint)
        cfg = _cfg(server, watch_interval=0)
        corpus, sessions = _world(tmp_path)
        for session in sessions:
            session.exited = False
        issue = (await corpus.service.board.issues(tmp_path))[0]
        # A link/task reference alone must never qualify either passage.
        sessions[0].sdef.issue = issue["id"]
        sessions[0].sdef.task = "issue: " + issue["id"]
        service = rag.RagService(config=lambda: cfg, root_dir=tmp_path / "index",
                                 manager=corpus.service.manager)
        service.all_docs = corpus.docs

        async def board_results():
            await service.wait_sync(service.ensure_sync("all", None), 10)
            result = await service.search("all", "board", mode="active", limit=50, wait=10, rerank=False)
            # Search starts another background pass on a populated index.
            # Finish it before the fixture mutates the next ownership field.
            await service.wait_sync(service.ensure_sync("all", None), 10)
            return [r for r in result["results"] if r["kind"] in ("beads", "comment")]

        async def assert_owned_group():
            rows = await board_results()
            assert len(rows) == 1
            assert rows[0]["issue"] == issue["id"]
            assert {rows[0]["kind"], *(m["kind"] for m in rows[0].get("matches", []))} == {"beads", "comment"}

        try:
            assert await board_results() == []
            issue["assignee"] = "s1"
            await assert_owned_group()
            issue["assignee"] = "unknown"
            assert await board_results() == []
            issue["created_by"] = "s2"
            await assert_owned_group()
            sessions[2].exited = True
            assert await board_results() == []
            # An active assignee still qualifies an issue with a dead creator.
            issue["assignee"] = "s1"
            await assert_owned_group()
            sessions[1].archived_at = "now"
            assert await board_results() == []
        finally:
            await service.shutdown()
            await server.close()
    asyncio.run(run())


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
        app.router.add_get("/api/search", api.h_search)
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
            for route in ("/api/search", "/api/search/stream"):
                response = await client.get(route, params={"q": "relay", "kind": "all", "mode": "active"})
                assert response.status == 200
                views = [v for _, v in _read_sse(await response.text())] if route.endswith("stream") else [await response.json()]
                assert views and all(v["results"] == [] for v in views)
                for params in ({"kind": "all", "mode": "invalid"}, {"kind": "sessions", "mode": "active"}):
                    response = await client.get(route, params={"q": "relay", **params})
                    assert response.status == 400
        finally:
            await service.shutdown()
            await client.close()
            await server.close()
    asyncio.run(run())


def test_import_current_reads_only_the_sessions_it_imports(monkeypatch):
    """The first Observer snapshot after a start ran ``import_current``,
    which read and decoded every record to learn which sessions already had
    a briefing or checks row -- 0.6 s of event loop on this machine
    (claunch-fh8u1). It asks the key for those sessions alone now."""
    from claude_launcher.daemon import briefing
    search_records.remember_many([
        (f"other{i}", [{"id": str(j), "kind": "observer", "text": "x", "at": str(j)} for j in range(50)])
        for i in range(20)
    ])
    status_checks.set_entries([{"id": "c1", "name": "tests", "question": "Tests pass?"}])
    status_checks.report("s1", [{"id": "c1", "answer": "yes"}])
    status_checks.report("s2", [{"id": "c1", "answer": "no"}])
    search_records.capture("s2", "checks", {"id": "c1", "answer": "old"}, "0")
    monkeypatch.setattr(briefing, "_restore_cache", lambda: None)
    monkeypatch.setattr(briefing, "_cache", {"s3": ((), {"briefing": {"goal": "g"}, "generated_at": "5"})})

    decoded = []
    real_loads = search_records.json.loads
    monkeypatch.setattr(search_records.json, "loads", lambda s, *a, **k: decoded.append(s) or real_loads(s, *a, **k))
    monkeypatch.setattr(search_records, "rows", lambda *a, **k: pytest.fail("import_current read every row"))

    search_records.import_current()
    # The status-check file is decoded too; no record payload is.
    assert not [s for s in decoded if '"origin": "record"' in s]
    assert search_records.kinds_present([("s1", "checks"), ("s2", "checks"), ("s3", "briefing"),
                                         ("s3", "checks"), ("other1", "checks")]) == {
        ("s1", "checks"), ("s2", "checks"), ("s3", "briefing")}

    # A second pass finds them all and writes nothing.
    monkeypatch.setattr(search_records, "capture", lambda *a, **k: pytest.fail("imported twice"))
    search_records.import_current()


def _rag_profile(name="ds4", *, provider="fireworks", api_key="fw-secret",
                 endpoints=None):
    """A profile whose provider serves an OpenAI-compatible API (claunch-dkrqf)."""
    from claude_launcher import profile
    profile.create(name)

    def mutate(doc):
        doc.setdefault("providers", {})[provider] = {
            "api_key": api_key,
            "endpoints": {"openai": "https://api.fireworks.ai/inference/v1"} if endpoints is None else endpoints,
            "models": {"default": "accounts/fireworks/models/deepseek-v4p1-flash"},
        }
        doc.setdefault("profiles", {}).setdefault(name, {})["provider"] = provider
    store.update(mutate)


def test_a_block_without_source_reads_as_the_typed_endpoint():
    cfg = store.rag_config({"rag": {"base_url": "http://localhost:8000/v1", "api_key": "k",
                                    "embedding_model": "e", "profile": "ds4"}})
    assert cfg["source"] == "custom"
    assert (cfg["base_url"], cfg["api_key"]) == ("http://localhost:8000/v1", "k")
    assert store.rag_configured(cfg)


def test_profile_source_uses_the_profile_backend_and_keeps_the_typed_pair():
    _rag_profile()
    store.update(lambda doc: doc.__setitem__("rag", {
        "base_url": "http://localhost:8000/v1", "api_key": "local-secret", "embedding_model": "embed"}))
    doc = search_anything.proposed({"source": "profile", "profile": "ds4", "api_key": ""})
    # Both paths stay in the file; only the choice moved.
    assert doc["rag"]["sources"]["custom"]["base_url"] == "http://localhost:8000/v1"
    assert doc["rag"]["sources"]["custom"]["api_key"] == "local-secret"
    cfg = store.rag_config(doc)
    assert cfg["source"] == "profile" and not cfg["profile_error"]
    assert cfg["base_url"] == "https://api.fireworks.ai/inference/v1"
    assert cfg["api_key"] == "fw-secret"
    store.save(doc)
    public = search_anything.public_settings()
    assert public["source"] == "profile" and public["profile"] == "ds4"
    assert public["base_url"] == "http://localhost:8000/v1"  # the form keeps the typed address
    assert public["resolved"] == {"base_url": "https://api.fireworks.ai/inference/v1", "has_key": True}
    assert public["configured"] and public["api_key_set"]
    assert [row["name"] for row in public["profiles"]] == ["ds4"]
    assert "fw-secret" not in str(public) and "local-secret" not in str(public)
    back = store.rag_config(search_anything.proposed({"source": "custom"}))
    assert (back["base_url"], back["api_key"]) == ("http://localhost:8000/v1", "local-secret")
    assert back["profile"] == "ds4"


def test_the_environment_key_belongs_to_the_custom_endpoint(monkeypatch):
    _rag_profile()
    monkeypatch.setenv("CLAUNCH_RAG_API_KEY", "env-secret")
    block = {"base_url": "http://localhost:8000/v1", "embedding_model": "e", "profile": "ds4"}
    assert store.rag_config({"rag": {**block, "source": "custom"}})["api_key"] == "env-secret"
    assert store.rag_config({**store.load(), "rag": {**block, "source": "profile"}})["api_key"] == "fw-secret"


def test_switching_the_connection_keeps_each_paths_model_ids():
    """claunch-pxxn4: the two connection paths keep their own model ids.

    A local oMLX server and a provider name their embedding models differently,
    and sharing one pair of fields meant that choosing a path overwrote the
    other path's names. Each row is now saved and read on its own, in both
    directions, and switching the radio does not need the unused path to have
    a usable address yet.
    """
    _rag_profile()
    store.save({**store.load(), "rag": {"sources": {
        "custom": {"base_url": "http://localhost:8000/v1", "api_key": "local-secret",
                   "embedding_model": "omlx-embed", "rerank_model": "omlx-rerank"},
        "profile": {"profile": "ds4", "embedding_model": "fw-embed", "rerank_model": ""},
    }, "source": "custom"}})
    # (a) profile -> custom: the custom path's ids are the ones that come back.
    store.save(search_anything.proposed({"source": "profile"}))
    assert store.rag_config()["embedding_model"] == "fw-embed"
    store.save(search_anything.proposed({"source": "custom"}))
    cfg = store.rag_config()
    assert cfg["source"] == "custom"
    assert (cfg["embedding_model"], cfg["rerank_model"]) == ("omlx-embed", "omlx-rerank")

    # (b) the other direction, with the custom ids replaced while in force.
    store.save(search_anything.proposed({"source": "custom", "embedding_model": "omlx-2"}))
    store.save(search_anything.proposed({"source": "profile"}))
    assert store.rag_config()["embedding_model"] == "fw-embed"
    store.save(search_anything.proposed({"source": "custom"}))
    assert store.rag_config()["embedding_model"] == "omlx-2"
    assert store.load()["rag"]["sources"]["profile"]["embedding_model"] == "fw-embed"

    # The card posts both rows; the path not in force keeps what it had.
    doc = search_anything.proposed({"source": "profile", "sources": {
        "profile": {"profile": "ds4", "embedding_model": "fw-2", "rerank_model": ""},
        "custom": {"base_url": "http://localhost:8000/v1", "embedding_model": "omlx-2",
                   "rerank_model": "omlx-rerank"}}})
    assert doc["rag"]["sources"]["custom"]["embedding_model"] == "omlx-2"
    assert store.rag_config(doc)["embedding_model"] == "fw-2"

    # A path with no address yet does not block choosing the other one.
    store.save({**store.load(), "rag": {"sources": {"profile": {"profile": "ds4", "embedding_model": "fw-embed"}},
                                        "source": "profile"}})
    assert search_anything.proposed({"source": "profile", "profile": "ds4"})["rag"]["source"] == "profile"


def test_a_legacy_block_feeds_both_rows_and_the_first_save_splits_them():
    """(c) A block written before the rows existed had one set of values.

    That one set is what both paths meant then, so both rows start from it --
    that reading is the migration. The first save writes the rows and drops the
    flat copies, so the two can then disagree without the flat copy going stale.
    """
    _rag_profile()
    store.save({**store.load(), "rag": {"base_url": "http://localhost:8000/v1", "api_key": "local-secret",
                                        "embedding_model": "legacy-embed", "rerank_model": "legacy-rerank",
                                        "profile": "ds4"}})
    rows = store.rag_source_rows(store.load()["rag"])
    assert rows["custom"]["embedding_model"] == "legacy-embed"
    assert rows["profile"]["embedding_model"] == "legacy-embed"
    assert rows["profile"]["profile"] == "ds4"
    assert rows["custom"]["base_url"] == "http://localhost:8000/v1"

    doc = search_anything.proposed({"source": "profile", "embedding_model": "fw-embed"})
    assert doc["rag"]["sources"]["custom"]["embedding_model"] == "legacy-embed"
    assert set(doc["rag"]) & {"base_url", "api_key", "profile", "embedding_model", "rerank_model"} == set()
    # The rows are now the only place those values live, and they disagree.
    assert store.rag_config(doc)["embedding_model"] == "fw-embed"
    assert store.rag_config({**doc, "rag": {**doc["rag"], "source": "custom"}})["embedding_model"] == "legacy-embed"


def test_a_stale_profile_name_does_not_block_a_custom_save():
    """The row out of force is kept, not validated (claunch-pxxn4 review).

    The card sends both rows on every save, so "the body carried the profile
    row" is always true and cannot mean "the profile path is the one being
    chosen". A profile the person deleted therefore used to block a custom
    save -- the reader had to fix a path they were not using. The stale name
    stays in the file so switching back restores it, and ``profile_error``
    diagnoses it at that point.
    """
    _rag_profile()
    store.save({**store.load(), "rag": {"sources": {
        "custom": {"base_url": "http://localhost:8000/v1", "api_key": "k",
                   "embedding_model": "omlx-embed", "rerank_model": ""},
        "profile": {"profile": "gone", "embedding_model": "fw-embed", "rerank_model": ""},
    }, "source": "profile"}})
    # The card's shape: both rows, the custom one chosen.
    doc = search_anything.proposed({"source": "custom", "sources": {
        "custom": {"base_url": "http://localhost:8000/v1", "api_key": "",
                   "embedding_model": "omlx-2", "rerank_model": ""},
        "profile": {"profile": "gone", "embedding_model": "fw-embed", "rerank_model": ""}}})
    assert doc["rag"]["sources"]["custom"]["embedding_model"] == "omlx-2"
    assert doc["rag"]["sources"]["profile"]["profile"] == "gone"  # kept, not dropped
    assert store.rag_config(doc)["embedding_model"] == "omlx-2"
    # Choosing that path is where the missing profile is reported.
    switched = {**doc, "rag": {**doc["rag"], "source": "profile"}}
    assert store.rag_config(switched)["profile_error"]
    with pytest.raises(ValueError):
        search_anything.proposed({"source": "profile"})


def test_a_legacy_stale_profile_name_does_not_block_the_first_save():
    """Same, for a block written before the rows existed: its flat ``profile``
    becomes the profile row's initial value, and that name may be gone. The
    first custom save must still go through."""
    _rag_profile()
    store.save({**store.load(), "rag": {"base_url": "http://localhost:8000/v1", "api_key": "k",
                                        "embedding_model": "embed", "profile": "gone"}})
    # The card's shape again: the first save after a legacy block carries both
    # rows, the profile one seeded from that flat ``profile``.
    doc = search_anything.proposed({"source": "custom", "sources": {
        "custom": {"base_url": "http://localhost:8000/v1", "api_key": "",
                   "embedding_model": "embed", "rerank_model": ""},
        "profile": {"profile": "gone", "embedding_model": "", "rerank_model": ""}}})
    assert doc["rag"]["sources"]["custom"]["embedding_model"] == "embed"
    assert doc["rag"]["sources"]["profile"]["profile"] == "gone"
    assert store.rag_config(doc)["embedding_model"] == "embed"


def test_profile_source_validation():
    _rag_profile()
    _rag_profile("anthropic-only", provider="anth", endpoints={"anthropic": "https://example.test/anthropic"})
    store.save({**store.load(), "rag": {"embedding_model": "e"}})
    # A profile-backed block needs no typed address.
    assert search_anything.proposed({"source": "profile", "profile": "ds4"})["rag"]["source"] == "profile"
    for bad in ({"source": "other"}, {"source": "profile"}, {"source": "profile", "profile": "no-such"},
                {"source": "profile", "profile": "anthropic-only"}, {"source": "custom"}):
        with pytest.raises(ValueError):
            search_anything.proposed(bad)
    rows = search_anything.profile_rows()
    assert [row["name"] for row in rows] == ["ds4"]
    cfg = store.rag_config({**store.load(), "rag": {"source": "profile", "profile": "no-such", "embedding_model": "e"}})
    assert cfg["profile_error"] and not store.rag_configured(cfg)


def test_settings_routes_call_the_profile_backend(tmp_path):
    async def run():
        endpoint = Endpoint()
        server = await _start_endpoint(endpoint)
        _rag_profile(endpoints={"openai": str(server.make_url("/v1"))})
        store.save({**store.load(), "rag": _cfg(server, watch_interval=0, base_url="http://localhost:9/v1")})
        service = rag.RagService(root_dir=tmp_path / "index")
        app = web.Application()
        app["rag"] = service
        app["observer"] = SimpleNamespace()
        search_anything.install(app)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            response = await client.post("/api/rag/test", json={"source": "profile", "profile": "ds4"})
            assert response.status == 200, await response.text()
            assert endpoint.headers[-1]["Authorization"] == "Bearer fw-secret"
            response = await client.put("/api/rag/settings", json={"source": "profile", "profile": "ds4"})
            assert response.status == 200
            text = await response.text()
            assert "fw-secret" not in text and "rag-secret" not in text
            assert store.load()["rag"]["sources"]["custom"]["base_url"] == "http://localhost:9/v1"
            assert store.rag_config()["base_url"] == str(server.make_url("/v1"))
        finally:
            await service.shutdown()
            await client.close()
            await server.close()
    asyncio.run(run())
