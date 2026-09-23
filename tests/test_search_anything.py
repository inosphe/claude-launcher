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
            # candidate window before the live task and unowned board record.
            for i in range(30):
                out += search_anything.documents(f"dead{i}", "relay", "relay", kind="checks", sessions=[{"name": "dead"}])
            for name in ("paused", "archived", "removed"):
                out += search_anything.documents(name, "relay", "relay", kind="opening-task", sessions=[{"name": name}])
            out += search_anything.documents("live-task", "relay task", "relay task details", kind="opening-task", sessions=[{"name": "live"}])
            out += search_anything.documents("board", "relay board", "relay closed issue", kind="beads", sessions=[])
            out += search_anything.documents("comment", "relay comment", "relay comment details", kind="comment", sessions=[{"name": "dead"}])
            return out
        service.all_docs = docs
        try:
            general = await service.search("all", "relay", limit=50, wait=10, rerank=False)
            assert any(r["id"].startswith("dead") for r in general["results"])
            active = await service.search("all", "relay", limit=3, mode="active", wait=10)
            assert {r["id"] for r in active["results"]} == {"live-task:0", "board:0", "comment:0"}
            assert all("dead" not in text for text in endpoint.rerank_calls[-1]["documents"])
            vector_only = await service.search("all", "relay unmatched-title-word", limit=2, mode="active", wait=0)
            assert len(vector_only["results"]) == 2
            assert all(not r["lexical"] and r["id"] in {"live-task:0", "board:0", "comment:0"}
                       for r in vector_only["results"])
            stream = service.search_stream("all", "relay", limit=3, mode="active", wait=0)
            _, first = await stream.__anext__()
            assert any(r["id"] == "live-task:0" for r in first["results"])
            live.exited = True
            _, reranked = await stream.__anext__()
            assert all(r["id"] != "live-task:0" for r in reranked["results"])
            await stream.aclose()
            # Reusing the same index must reflect process exit without a sync.
            active = await service.search("all", "relay", limit=3, mode="active", wait=0)
            assert {r["id"] for r in active["results"]} == {"board:0", "comment:0"}
            service.manager = None
            active = await service.search("all", "relay", mode="active", wait=0)
            assert {r["kind"] for r in active["results"]} == {"beads", "comment"}
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
