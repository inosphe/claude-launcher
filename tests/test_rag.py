"""Semantic search (daemon/rag.py): the config block, the index, the
endpoint client, the service and its routes, and the CLI.

The endpoint is a real local aiohttp server answering ``/embeddings`` and
``/rerank`` in the shapes the measured endpoint uses (OpenAI embeddings,
Cohere rerank), the same arrangement ``test_briefing`` uses for the LLM —
so the request contract is pinned by what arrives, not by a mock's memory.
Vectors are deterministic: a text's embedding is a one-hot of its first
word, so "which document ranks first" is arithmetic the test can predict.
"""

from __future__ import annotations

import asyncio
import json
import threading
import time
from pathlib import Path

import pytest
from aiohttp import web as aioweb
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import store
from claude_launcher.daemon import beads as beads_mod
from claude_launcher.daemon import paths, rag
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

BEARER = {"Authorization": "Bearer sekrit"}
DIMS = 8
WORDS = ["kanban", "pytest", "relay", "briefing", "spawn", "window", "mesh", "beads"]


# --------------------------------------------------------------------------- #
# the endpoint, in-process
# --------------------------------------------------------------------------- #
def _vector_for(text: str):
    """A unit vector keyed by the first known word in the text (else the last slot)."""
    slot = DIMS - 1
    for word in text.lower().split():
        word = word.strip("[]:,.")
        if word in WORDS:
            slot = WORDS.index(word)
            break
    vec = [0.0] * DIMS
    vec[slot] = 1.0
    return vec


class Endpoint:
    """The two calls the module makes, recording what it sent."""

    def __init__(self):
        self.embed_calls: list = []
        self.rerank_calls: list = []
        self.headers: list = []
        self.fail_embed = False

    async def embeddings(self, request):
        body = await request.json()
        self.embed_calls.append(body)
        self.headers.append(dict(request.headers))
        if self.fail_embed:
            return aioweb.json_response({"error": "boom"}, status=500)
        rows = [{"index": i, "embedding": _vector_for(t)} for i, t in enumerate(body["input"])]
        return aioweb.json_response({"object": "list", "data": rows, "model": body["model"],
                                     "usage": {"prompt_tokens": 1, "total_tokens": 1}})

    async def rerank(self, request):
        body = await request.json()
        self.rerank_calls.append(body)
        # The document mentioning the query's first word wins; then by index.
        word = body["query"].split()[0].lower()
        results = []
        for i, doc in enumerate(body["documents"]):
            score = 0.9 if word in doc.lower() else 0.1 / (i + 1)
            results.append({"index": i, "relevance_score": score, "document": {"text": doc}})
        results.sort(key=lambda r: -r["relevance_score"])
        return aioweb.json_response({"id": "rerank-1", "results": results[: body.get("top_n", 10)],
                                     "model": body["model"], "usage": {"total_tokens": 1}})


async def _start_endpoint(ep: Endpoint) -> TestServer:
    app = aioweb.Application()
    app.router.add_post("/v1/embeddings", ep.embeddings)
    app.router.add_post("/v1/rerank", ep.rerank)
    server = TestServer(app)
    await server.start_server()
    return server


def _cfg(server: TestServer, **over) -> dict:
    cfg = dict(store.RAG_DEFAULTS)
    cfg.update({
        "base_url": str(server.make_url("/v1")),
        "api_key": "rag-secret",
        "embedding_model": "emb",
        "rerank_model": "rr",
        "batch": 4,
        "candidates": 6,
        "rerank_top": 3,
    })
    cfg.update(over)
    return cfg


# --------------------------------------------------------------------------- #
# the board, in memory (what the daemon's Board asks br for)
# --------------------------------------------------------------------------- #
class FakeBr:
    def __init__(self, issues):
        self.issues = issues
        self.calls = []

    async def __call__(self, argv, cwd):
        self.calls.append(argv)
        args = [a for a in argv if a != "--json"]
        if "list" in args:
            return 0, json.dumps(self.issues), ""
        if "update" in args:
            return 0, "{}", ""
        return 1, "", "unknown"


def _issues():
    return [
        {"id": "x-1", "title": "kanban board gets an in_ready lane", "status": "open",
         "priority": 2, "labels": ["ui"], "description": "kanban lanes\n\nadd the column",
         "updated_at": "2026-09-01T00:00:00Z"},
        {"id": "x-2", "title": "pytest basetemp permission error", "status": "closed",
         "priority": 3, "labels": [], "description": "pytest fails on Temp",
         "updated_at": "2026-09-01T00:00:00Z"},
        {"id": "x-3", "title": "relay uplink drops on restart", "status": "open",
         "priority": 1, "labels": ["daemon"], "description": "relay reconnects late",
         "updated_at": "2026-09-01T00:00:00Z"},
    ]


def _board(br, root):
    """The board under ``root`` — and no board anywhere else."""
    def root_for(cwd):
        return root if cwd and str(cwd).startswith(str(root)) else None
    return beads_mod.Board(br, root_for=root_for)


@pytest.fixture
def repo(tmp_path):
    root = tmp_path / "repo"
    (root / ".beads").mkdir(parents=True)
    (root / ".beads" / "beads.db").write_bytes(b"")
    return root


# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
def test_rag_config_absent_block_is_disabled_defaults():
    cfg = store.rag_config({})
    assert cfg == store.RAG_DEFAULTS
    assert not store.rag_configured(cfg)
    assert store.rag_config({"rag": "nope"}) == store.RAG_DEFAULTS


def test_rag_config_reads_the_block_and_coerces(monkeypatch):
    monkeypatch.delenv("CLAUNCH_RAG_API_KEY", raising=False)
    cfg = store.rag_config({"rag": {
        "base_url": " https://h/v1 ", "api_key": "k", "embedding_model": "e",
        "rerank_model": "r", "verify_tls": False, "dimensions": "1024",
        "batch": "nope", "timeout": "30", "rerank_top": 0,
    }})
    assert cfg["base_url"] == "https://h/v1"
    assert cfg["verify_tls"] is False
    assert cfg["dimensions"] == 1024
    assert cfg["batch"] == store.RAG_DEFAULTS["batch"]
    assert cfg["timeout"] == 30.0
    assert cfg["rerank_top"] == 1
    assert store.rag_configured(cfg)
    # rerank is optional: the feature is on without it
    assert store.rag_configured(store.rag_config({"rag": {
        "base_url": "https://h/v1", "api_key": "k", "embedding_model": "e"}}))


def test_rag_config_env_key_overrides_the_file(monkeypatch):
    monkeypatch.setenv("CLAUNCH_RAG_API_KEY", "from-env")
    cfg = store.rag_config({"rag": {"base_url": "https://h/v1", "embedding_model": "e"}})
    assert cfg["api_key"] == "from-env"
    assert store.rag_configured(cfg)


def test_rag_config_reads_the_store():
    store.update(lambda doc: doc.update({"rag": {
        "base_url": "https://h/v1", "api_key": "k", "embedding_model": "e"}}))
    assert store.rag_config()["embedding_model"] == "e"


def test_rag_config_watch_interval_zero_is_off_not_default():
    assert store.rag_config({})["watch_interval"] == store.RAG_DEFAULTS["watch_interval"]
    assert store.rag_config({"rag": {"watch_interval": 0}})["watch_interval"] == 0.0
    assert store.rag_config({"rag": {"watch_interval": "5"}})["watch_interval"] == 5.0
    assert store.rag_config({"rag": {"watch_interval": -3}})["watch_interval"] == 0.0
    assert store.rag_config({"rag": {"watch_interval": "x"}})["watch_interval"] == 30.0


# --------------------------------------------------------------------------- #
# documents and the index
# --------------------------------------------------------------------------- #
def test_chunk_text_keeps_the_head_on_every_chunk_and_caps():
    body = "\n\n".join(f"para {i} " + "x" * 900 for i in range(40))
    chunks = rag.chunk_text("TITLE", body)
    assert len(chunks) == rag.MAX_CHUNKS
    assert all(c.startswith("TITLE\n") for c in chunks)
    assert all(len(c) <= rag.CHUNK_CHARS + 1 for c in chunks)
    assert rag.chunk_text("TITLE", "") == ["TITLE"]
    # a paragraph longer than a chunk is cut rather than dropped
    assert len(rag.chunk_text("T", "y" * 7000)) == 3


def test_issue_doc_hash_follows_the_text_not_the_status():
    a = rag.issue_doc({"id": "i", "title": "t", "description": "d", "status": "open"})
    b = rag.issue_doc({"id": "i", "title": "t", "description": "d", "status": "closed"})
    c = rag.issue_doc({"id": "i", "title": "t", "description": "d2", "status": "open"})
    assert a.hash == b.hash != c.hash
    assert a.meta["status"] == "open" and b.meta["status"] == "closed"
    assert a.chunks[0].startswith("t\n")
    labelled = rag.issue_doc({"id": "i", "title": "t", "labels": ["a", "b"], "description": ""})
    assert labelled.chunks == ["t\n[a b]"]


def test_session_doc_reads_task_identity_and_briefing():
    doc = rag.session_doc(
        {"name": "s9", "task": "Build the widget", "identity": "worker w1", "issue": "x-1",
         "cwd": "/r", "status": "idle"},
        {"one-line-job-description": "building widget", "state": "working", "goal": "ship"},
    )
    assert doc.id == "s9"
    assert "identity: worker w1" in doc.chunks[0]
    assert "task:\nBuild the widget" in doc.chunks[0]
    assert "goal: ship" in doc.chunks[0]
    assert doc.meta["one_line"] == "building widget" and doc.meta["state"] == "working"
    assert doc.meta["excerpt"] == "Build the widget"


def test_session_doc_carries_the_readers_own_note():
    """The note is how a session gets found again once its own name has
    stopped meaning anything, so it is searchable text like the task is.

    This corpus and the unified one (search_anything.Corpus) are two
    hand-written answers to "what makes up a session's searchable text"; a
    field added to one and not the other leaves the session findable by its
    note in Search anything and not in the rail's semantic search. This pins
    this side of that pair.
    """
    doc = rag.session_doc(
        {"name": "s9", "note": "  waiting on the vendor  ", "identity": "worker w1"}
    )
    chunk = doc.chunks[0]
    assert "note: waiting on the vendor" in chunk
    # It is the reader's line about the session, so it reads before the
    # record's own fields rather than after them.
    assert chunk.index("note:") < chunk.index("identity:")
    # A session nobody annotated writes no such line, rather than an empty one.
    bare = rag.session_doc({"name": "s9", "note": "   "})
    assert "note:" not in bare.chunks[0]


def test_vector_index_round_trips_and_diffs(tmp_path):
    path = tmp_path / "idx.json"
    index = rag.VectorIndex(path, model="m", dims=0)
    index.load()
    d1 = rag.Doc("a", "h1", ["a one", "a two"], {"title": "A"})
    d2 = rag.Doc("b", "h2", ["b one"], {"title": "B"})
    index.put(d1, [rag.unit([1, 0, 0]), rag.unit([0, 1, 0])])
    index.put(d2, [rag.unit([0, 0, 1])])
    index.save()
    again = rag.VectorIndex(path, model="m", dims=0)
    again.load()
    assert set(again.entries) == {"a", "b"}
    assert again.dims == 3
    assert list(again.entries["a"].vecs[1]) == pytest.approx([0, 1, 0])
    # the best chunk decides the document's score
    ranked = again.rank(rag.unit([0, 1, 0]), 5)
    assert ranked[0][0] == "a" and ranked[0][1] == pytest.approx(1.0)
    assert again.neighbors("b", 5)[0][0] == "a"
    # diff: a changed hash re-embeds, a missing id drops
    stale, gone = again.diff([rag.Doc("a", "h1x", ["a"], {}), rag.Doc("c", "h3", ["c"], {})])
    assert [d.id for d in stale] == ["a", "c"] and gone == ["b"]
    # metadata refresh without re-embedding
    again.refresh_meta(rag.Doc("a", "h1", ["a"], {"title": "A2"}))
    assert again.entries["a"].meta["title"] == "A2"
    # another model's file is not mixed in
    other = rag.VectorIndex(path, model="m2", dims=0)
    other.load()
    assert other.entries == {}
    assert again.lexical("A2", 5) == ["a"]


# --------------------------------------------------------------------------- #
# the client
# --------------------------------------------------------------------------- #
def test_client_sends_the_openai_and_cohere_shapes():
    ep = Endpoint()

    async def run():
        server = await _start_endpoint(ep)
        try:
            client = rag.RagClient(_cfg(server, dimensions=512, batch=2))
            vecs = await client.embed(["kanban a", "pytest b", "relay c"])
            assert len(vecs) == 3 and vecs[0][0] == pytest.approx(1.0)
            # batched by `batch`, dimensions passed through, bearer + model on the wire
            assert [len(c["input"]) for c in ep.embed_calls] == [2, 1]
            assert ep.embed_calls[0]["model"] == "emb" and ep.embed_calls[0]["dimensions"] == 512
            assert ep.headers[0]["Authorization"] == "Bearer rag-secret"
            pairs = await client.rerank("pytest question", ["kanban doc", "pytest doc"], 2)
            assert pairs[0] == (1, pytest.approx(0.9))
            assert ep.rerank_calls[0] == {"model": "rr", "query": "pytest question",
                                          "documents": ["kanban doc", "pytest doc"], "top_n": 2}
            # no rerank model: no call, empty answer
            silent = rag.RagClient(_cfg(server, rerank_model=""))
            assert await silent.rerank("q", ["d"], 1) == []
            assert len(ep.rerank_calls) == 1
            # a failing endpoint is an error that never carries the key
            ep.fail_embed = True
            with pytest.raises(rag.RagError) as err:
                await client.embed(["x"])
            assert "500" in str(err.value) and "rag-secret" not in str(err.value)
        finally:
            await server.close()

    asyncio.run(run())


def test_session_verifies_via_the_os_trust_store_unless_disabled():
    """``verify_tls`` true (the default) hands the connector the OS-backed
    context when ``truststore`` is installed — the fix for a corporate
    TLS-inspection root that OpenSSL's own chain validation rejects but the
    OS already trusts (claunch-go5a). ``verify_tls: false`` still turns
    verification off outright, unaffected by that context's availability."""
    async def run():
        cfg = dict(store.RAG_DEFAULTS, base_url="https://x", api_key="k", embedding_model="m")
        async with rag.RagClient(cfg)._session() as verified:
            if rag._OS_TRUST_CONTEXT is not None:
                assert verified.connector._ssl is rag._OS_TRUST_CONTEXT
            else:
                assert verified.connector._ssl is True
        async with rag.RagClient(dict(cfg, verify_tls=False))._session() as unverified:
            assert unverified.connector._ssl is False

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the service
# --------------------------------------------------------------------------- #
def test_service_indexes_incrementally_and_ranks(tmp_path, repo):
    ep = Endpoint()
    issues = _issues()
    br = FakeBr(issues)

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server)
            svc = rag.RagService(board=_board(br, repo), config=lambda: cfg, root_dir=tmp_path / "rag")
            view = await svc.search("beads", "pytest permission", root=repo, limit=2)
            assert view["index"]["total"] == 3 and view["index"]["indexed"] == 3
            assert view["index"]["pending"] == 0 and not view["index"]["syncing"]
            assert [r["id"] for r in view["results"]] == ["x-2", "x-1"] or \
                [r["id"] for r in view["results"]][0] == "x-2"
            top = view["results"][0]
            assert top["title"] == "pytest basetemp permission error"
            assert top["status"] == "closed" and top["rerank_score"] == pytest.approx(0.9)
            assert view["reranked"] is True
            # documents embedded in one pass: 3 issues, 1 chunk each, + 1 query
            embedded = [t for call in ep.embed_calls for t in call["input"]]
            assert len(embedded) == 4
            # the index file exists and a fresh service reads it without re-embedding
            files = list((tmp_path / "rag").glob("beads-*.json"))
            assert len(files) == 1
            before = len(ep.embed_calls)
            fresh = rag.RagService(board=_board(br, repo), config=lambda: cfg, root_dir=tmp_path / "rag")
            view2 = await fresh.search("beads", "relay", root=repo, limit=1, rerank=False)
            assert view2["results"][0]["id"] == "x-3" and view2["reranked"] is False
            assert len(ep.embed_calls) == before + 1  # the query only
            # The search no longer waits on the sync it started, so settle
            # that pass before changing the board -- otherwise the sync
            # below joins the running one, which read the old issues.
            await fresh.drain(5)
            # one issue changes text: only that one is re-embedded
            issues[0]["description"] = "kanban lanes rewritten"
            br.issues = issues
            fresh.board._cache.clear()
            prog = fresh.ensure_sync("beads", repo)
            await fresh.wait_sync(prog, 5)
            assert prog.pending == 0 and prog.indexed == 3
            assert ep.embed_calls[-1]["input"][0].startswith("kanban board gets")
            # lexical: an id the vectors would not surface comes first
            view3 = await fresh.search("beads", "x-3", root=repo, limit=2, rerank=False)
            assert view3["results"][0]["id"] == "x-3" and view3["results"][0]["lexical"] is True
            # related: the neighbours of one issue, never itself
            rel = await fresh.related(repo, "x-1", limit=2)
            assert rel["indexed"] is True
            assert "x-1" not in [r["id"] for r in rel["results"]]
            assert len(rel["results"]) == 2
            status = fresh.status()
            assert status["configured"] and status["indexes"][0]["documents"] == 3
            assert "rag-secret" not in json.dumps(status)
        finally:
            await server.close()

    asyncio.run(run())


def test_service_refuses_when_unconfigured_and_reports_endpoint_errors(tmp_path, repo):
    ep = Endpoint()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            off = rag.RagService(board=_board(br, repo), config=lambda: dict(store.RAG_DEFAULTS),
                                 root_dir=tmp_path / "rag")
            assert not off.configured()
            with pytest.raises(rag.RagError):
                await off.search("beads", "x", root=repo)
            prog = off.ensure_sync("beads", repo)
            assert prog.error and prog.task is None
            ep.fail_embed = True
            cfg = _cfg(server)
            svc = rag.RagService(board=_board(br, repo), config=lambda: cfg, root_dir=tmp_path / "rag")
            prog = svc.ensure_sync("beads", repo)
            await svc.wait_sync(prog, 5)
            assert prog.error and "500" in prog.error and "rag-secret" not in prog.error
            with pytest.raises(rag.RagError):
                await svc.search("beads", "x", root=repo, wait=0)
            with pytest.raises(ValueError):
                await svc.search("nope", "x")
        finally:
            await server.close()

    asyncio.run(run())


def test_sessions_corpus_reads_the_registry_and_briefing_cache(tmp_path):
    ep = Endpoint()

    class Sdef:
        def __init__(self, name, task, identity=None, issue=None, note=None):
            self.name, self.task, self.identity, self.issue = name, task, identity, issue
            self.note = note
            self.role = None
            self.cwd = "/r"

    class Sess:
        def __init__(self, sdef, status="idle"):
            self.sdef = sdef
            self._status = status

        def status(self):
            return self._status

    class Manager:
        def list(self):
            return [Sess(Sdef("s1", "kanban lane work", identity="worker w1", issue="x-1",
                              note="waiting on the vendor reply")),
                    Sess(Sdef("s2", "pytest cleanup"), status="exited")]

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server)
            briefs = {"s2": {"one-line-job-description": "cleaning pytest", "state": "done"}}
            svc = rag.RagService(manager=Manager(), config=lambda: cfg, root_dir=tmp_path / "rag",
                                 briefing_for=briefs.get)
            view = await svc.search("sessions", "pytest", limit=2, rerank=False)
            assert view["kind"] == "sessions" and view["root"] is None
            assert view["results"][0]["id"] == "s2"
            assert view["results"][0]["one_line"] == "cleaning pytest"
            assert view["results"][0]["status"] == "exited"
            first = [r for r in view["results"] if r["id"] == "s1"][0]
            assert first["identity"] == "worker w1" and first["issue"] == "x-1"
            # A session is findable by the note the reader wrote on it. The
            # note is the one field here a person typed, and this corpus is
            # the rail's semantic search — a session searchable by its note
            # in Search anything and not here would be the two builders
            # disagreeing (see session_doc's own note). "vendor" appears
            # nowhere in s1's own fields, so ranking it first for that word
            # is the note doing the work.
            by_note = await svc.search("sessions", "vendor", limit=2, rerank=False)
            assert by_note["results"][0]["id"] == "s1"
            assert (tmp_path / "rag" / "sessions.json").is_file()
        finally:
            await server.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the queue: producers, one consumer, the board-file watcher
# --------------------------------------------------------------------------- #
def _embedded_docs(ep):
    """Every text the endpoint was asked to embed, in order."""
    return [t for call in ep.embed_calls for t in call["input"]]


def test_queue_coalesces_same_key_and_sync_is_idempotent(tmp_path, repo):
    """(a) five enqueues of one key run one sync; (c) a second sync of an
    unchanged corpus makes no embedding call; (f) unconfigured, the queue
    does not grow."""
    ep = Endpoint()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg_box = {"cfg": dict(store.RAG_DEFAULTS)}
            svc = rag.RagService(board=_board(br, repo), config=lambda: cfg_box["cfg"],
                                 root_dir=tmp_path / "rag")
            # (f) off: every producer is a no-op, nothing is queued, no task
            assert svc.enqueue("beads", repo) is False
            assert svc.enqueue("sessions") is False
            svc.on_board_write(repo)
            svc.on_sessions_changed()
            assert svc.queue_view()["depth"] == 0 and svc.queue_view()["pending"] == []
            assert svc._consumer is None
            # (a) on: five enqueues before the consumer runs → one pass
            cfg_box["cfg"] = _cfg(server)
            results = [svc.enqueue("beads", repo) for _ in range(5)]
            assert results == [True, False, False, False, False]
            assert svc.queue_view()["depth"] == 1
            await svc.drain()
            prog = svc._progress_of(svc._key("beads", repo))
            assert prog.runs == 1 and prog.indexed == 3 and prog.pending == 0
            assert len(_embedded_docs(ep)) == 3
            assert svc.queue_view()["consumed"] == 1
            assert svc.queue_view()["last_consumed_key"] == svc._key("beads", repo)
            assert svc.queue_view()["last_consumed_at"]
            # (c) idempotent: another pass over the same board embeds nothing
            svc.board._cache.clear()
            assert svc.enqueue("beads", repo) is True
            await svc.drain()
            assert prog.runs == 2 and len(_embedded_docs(ep)) == 3
            # and a search afterwards answers from the index the queue built
            view = await svc.search("beads", "relay", root=repo, limit=1, rerank=False, wait=0)
            assert view["results"][0]["id"] == "x-3"
            assert len(_embedded_docs(ep)) == 4  # the query only
        finally:
            await svc.shutdown()
            await server.close()

    asyncio.run(run())


def test_queue_reruns_a_key_enqueued_mid_sync(tmp_path, repo):
    """(b) an enqueue that lands while its key's sync is running is not
    lost: the key runs once more after the current pass."""
    ep = Endpoint()
    issues = _issues()
    br = FakeBr(issues)

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server)
            svc = rag.RagService(board=_board(br, repo), config=lambda: cfg, root_dir=tmp_path / "rag")
            gate = asyncio.Event()
            reading = asyncio.Event()
            real_docs = svc._docs

            async def slow_docs(kind, root):
                reading.set()
                await gate.wait()
                return await real_docs(kind, root)

            svc._docs = slow_docs
            assert svc.enqueue("beads", repo) is True
            await asyncio.wait_for(reading.wait(), 5)
            # mid-sync: the key is no longer pending, so it queues again
            assert svc.queue_view()["pending"] == []
            assert svc.enqueue("beads", repo) is True
            assert svc.enqueue("beads", repo) is False  # joins the waiting one
            assert svc.queue_view()["depth"] == 1
            # the change that landed mid-sync
            issues.append({"id": "x-4", "title": "mesh relay retry", "status": "open",
                           "priority": 2, "labels": [], "description": "mesh", "updated_at": "z"})
            br.issues = issues
            svc.board._cache.clear()
            gate.set()
            await svc.drain()
            prog = svc._progress_of(svc._key("beads", repo))
            assert prog.runs == 2
            index = svc._index("beads", repo, cfg)
            assert "x-4" in index.entries
            # one key never syncs twice at once: the second pass started after the first ended
            assert svc.queue_view()["consumed"] == 2
        finally:
            await svc.shutdown()
            await server.close()

    asyncio.run(run())


def test_board_write_hook_enqueues_and_restamps(tmp_path, repo):
    """(e) a ``br`` write through Board.br reaches the queue (and a read
    does not); the daemon's own write does not trip the watcher again."""
    ep = Endpoint()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server)
            board = _board(br, repo)
            svc = rag.RagService(board=board, config=lambda: cfg, root_dir=tmp_path / "rag")
            board.write_hooks.append(svc.on_board_write)
            seen = []
            board.write_hooks.append(seen.append)
            await board.issues(repo)  # a read
            assert seen == [] and svc.queue_view()["depth"] == 0
            (repo / ".beads" / "beads.db").write_bytes(b"written by br")
            await board.br(repo, ["update", "x-1", "--status", "closed"], actor="s1")
            assert seen == [repo]
            assert svc.queue_view()["pending"] == [svc._key("beads", repo)]
            # the stamp taken after the write equals the file now, so the
            # watcher's next tick sees nothing to queue
            assert svc._stamps[str(repo)] == rag.board_stamp(repo)
            assert svc._watch_tick() == []
            await svc.drain()
            assert svc._progress_of(svc._key("beads", repo)).runs == 1
        finally:
            await svc.shutdown()
            await server.close()

    asyncio.run(run())


def test_watcher_catches_a_cli_write_by_mtime(tmp_path, repo):
    """(d) a write that bypassed the daemon (``claunch beads …``) moves
    ``beads.db``; the watcher sees it within one interval, drops the board's
    cache and the new issue lands in the index."""
    import os
    ep = Endpoint()
    issues = _issues()
    br = FakeBr(issues)

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server, watch_interval=0.05)
            board = _board(br, repo)
            svc = rag.RagService(board=board, config=lambda: cfg, root_dir=tmp_path / "rag")
            svc.watch(repo)
            svc.start()
            # the boot catch-up on the first tick indexes the board as it is
            await asyncio.sleep(0.15)
            await svc.drain()
            assert svc.queue_view()["watcher"] is True
            assert svc.queue_view()["watch_interval"] == 0.05
            prog = svc._progress_of(svc._key("beads", repo))
            assert prog.runs >= 1 and prog.total == 3
            runs_before = prog.runs
            # a CLI write: the board answers differently and the db file moved
            await board.issues(repo)  # warm the daemon's cache with the old listing
            issues.append({"id": "x-9", "title": "window reservation fairness", "status": "open",
                           "priority": 2, "labels": [], "description": "window", "updated_at": "z"})
            br.issues = issues
            db = repo / ".beads" / "beads.db"
            db.write_bytes(b"cli wrote")
            os.utime(db, ns=(time.time_ns() + 2_000_000_000,) * 2)
            started = time.monotonic()
            for _ in range(200):
                await asyncio.sleep(0.02)
                index = svc._indexes.get(svc._key("beads", repo))
                if index is not None and "x-9" in index.entries and not svc._pending:
                    break
            elapsed = time.monotonic() - started
            assert "x-9" in svc._index("beads", repo, cfg).entries
            assert elapsed < 2.0
            assert prog.runs > runs_before and prog.total == 4
            # only the new issue was embedded
            assert _embedded_docs(ep)[-1].startswith("window reservation")
        finally:
            await svc.shutdown()
            await server.close()

    asyncio.run(run())


def test_sessions_producers_follow_the_registry_and_briefing_cache(tmp_path):
    """The fleet corpus is queued when the registry persists and when a
    briefing is cached — the same hook, either signature."""
    from claude_launcher.daemon import briefing
    ep = Endpoint()

    class Sess:
        def __init__(self, name):
            class Sdef:
                pass
            self.sdef = Sdef()
            self.sdef.name, self.sdef.task, self.sdef.identity = name, "kanban lanes", None
            self.sdef.role = self.sdef.issue = None
            self.sdef.cwd = "/r"

        def status(self):
            return "idle"

    class Manager:
        def __init__(self):
            self.sessions = [Sess("s1")]

        def list(self):
            return list(self.sessions)

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server)
            mgr = Manager()
            svc = rag.RagService(manager=mgr, config=lambda: cfg, root_dir=tmp_path / "rag",
                                 briefing_for=lambda name: None)
            briefing.persist_hooks.append(svc.on_sessions_changed)
            try:
                svc.on_sessions_changed()          # the registry's change hook
                await svc.drain()
                assert svc._progress_of("sessions").runs == 1
                assert "s1" in svc._index("sessions", None, cfg).entries
                mgr.sessions.append(Sess("s2"))
                svc.on_sessions_changed(mgr.sessions[-1])  # the exit hook's signature
                await svc.drain()
                assert "s2" in svc._index("sessions", None, cfg).entries
                before = svc._progress_of("sessions").runs
                briefing._persist_cache()          # the briefing cache's hook
                await svc.drain()
                assert svc._progress_of("sessions").runs == before + 1
                assert len(_embedded_docs(ep)) == 2  # s1, s2 — the last pass embedded nothing
            finally:
                briefing.persist_hooks.remove(svc.on_sessions_changed)
        finally:
            await svc.shutdown()
            await server.close()

    asyncio.run(run())


def test_app_wires_the_producers_and_shutdown_cancels_the_tasks(tmp_path, repo, monkeypatch):
    """(g) build_app hangs the three producers on the board, the registry and
    the briefing cache, starts the watcher with the app, and its shutdown
    hook cancels the watcher and the consumer; the status route carries the
    queue."""
    from claude_launcher.daemon import briefing
    ep = Endpoint()
    br = FakeBr(_issues())
    monkeypatch.chdir(repo)

    async def run():
        server = await _start_endpoint(ep)
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
        board = _board(br, repo)
        cfg = _cfg(server, watch_interval=0.05)
        service = rag.RagService(board=board, manager=mgr, config=lambda: cfg,
                                 root_dir=tmp_path / "rag")
        client = await _serve(mgr, board, service)
        try:
            assert service.on_board_write in board.write_hooks
            assert service.on_sessions_changed in mgr.change_hooks
            assert service.on_sessions_changed in mgr.exit_hooks
            assert service.on_sessions_changed in briefing.persist_hooks
            # on_startup started the watcher; its first tick queued the fleet
            await asyncio.sleep(0.1)
            await service.drain()
            assert service._watcher is not None and not service._watcher.done()
            assert service._progress_of("sessions").runs >= 1
            # a board write through the daemon's Board reaches the queue
            (repo / ".beads" / "beads.db").write_bytes(b"x")
            await board.br(repo, ["update", "x-1", "--status", "closed"], actor="s1")
            await service.drain()
            assert service._progress_of(service._key("beads", repo)).runs >= 1
            resp = await client.get("/api/rag/status", headers=BEARER)
            status = await resp.json()
            q = status["queue"]
            assert q["depth"] == 0 and q["pending"] == [] and q["consumer"] is True
            assert q["watcher"] is True and q["watch_interval"] == 0.05
            assert q["consumed"] >= 2 and q["last_consumed_at"]
            assert str(repo) in q["watched"]
            assert "rag-secret" not in json.dumps(status)
            consumer, watcher = service._consumer, service._watcher
        finally:
            await client.close()   # runs app.on_shutdown
            await server.close()
        assert consumer.done() and watcher.done()
        assert service._consumer is None and service._watcher is None
        assert service.on_sessions_changed not in briefing.persist_hooks
        assert service.queue_view()["consumer"] is False and service.queue_view()["watcher"] is False

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the routes
# --------------------------------------------------------------------------- #
async def _serve(mgr, board, service):
    app = build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr),
                    beads=board, rag=service)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


def test_search_routes_contract(tmp_path, repo, monkeypatch):
    ep = Endpoint()
    br = FakeBr(_issues())
    monkeypatch.chdir(repo)

    async def run():
        server = await _start_endpoint(ep)
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
        board = _board(br, repo)
        cfg_box = {"cfg": dict(store.RAG_DEFAULTS)}
        service = rag.RagService(board=board, manager=mgr, config=lambda: cfg_box["cfg"],
                                 root_dir=tmp_path / "rag")
        client = await _serve(mgr, board, service)
        try:
            # off: the list says so, the routes refuse with 400
            resp = await client.get("/api/sessions?view=rail", headers=BEARER)
            assert (await resp.json())["rag_configured"] is False
            resp = await client.get("/api/search?q=kanban", headers=BEARER)
            assert resp.status == 400
            resp = await client.get("/api/rag/status", headers=BEARER)
            assert resp.status == 200 and (await resp.json())["configured"] is False
            # on
            cfg_box["cfg"] = _cfg(server)
            resp = await client.get("/api/sessions?view=rail", headers=BEARER)
            assert (await resp.json())["rag_configured"] is True
            resp = await client.get("/api/search?q=", headers=BEARER)
            assert resp.status == 400
            resp = await client.get("/api/search?q=x&kind=nope", headers=BEARER)
            assert resp.status == 400
            resp = await client.get("/api/search?q=x&limit=0", headers=BEARER)
            assert resp.status == 400
            resp = await client.get(f"/api/search?q=relay+restart&cwd={repo}&limit=2", headers=BEARER)
            assert resp.status == 200
            view = await resp.json()
            assert view["kind"] == "beads" and view["results"][0]["id"] == "x-3"
            assert view["index"]["indexed"] == 3 and view["reranked"] is True
            assert view["root"] == str(repo)
            # no board here
            resp = await client.get(f"/api/search?q=x&cwd={tmp_path / 'nowhere'}", headers=BEARER)
            assert resp.status == 404
            # sessions corpus needs no cwd
            resp = await client.get("/api/search?q=anything&kind=sessions&rerank=0", headers=BEARER)
            assert resp.status == 200 and (await resp.json())["kind"] == "sessions"
            # related
            resp = await client.get(f"/api/beads/x-1/related?cwd={repo}&limit=1", headers=BEARER)
            assert resp.status == 200
            rel = await resp.json()
            assert rel["id"] == "x-1" and len(rel["results"]) == 1 and rel["results"][0]["id"] != "x-1"
            # status lists the loaded indexes, never the key
            resp = await client.get("/api/rag/status", headers=BEARER)
            status = await resp.json()
            assert status["configured"] and {i["kind"] for i in status["indexes"]} == {"beads", "sessions"}
            assert "rag-secret" not in json.dumps(status)
            # reindex: 202 and a progress view; force re-embeds
            before = len(ep.embed_calls)
            resp = await client.post("/api/rag/reindex", json={"cwd": str(repo), "force": True}, headers=BEARER)
            assert resp.status == 202
            body = await resp.json()
            assert body["kind"] == "beads" and body["index"]["total"] in (0, 3)
            await service.wait_sync(service._progress_of(service._key("beads", repo)), 5)
            assert len(ep.embed_calls) > before
            resp = await client.post("/api/rag/reindex", json={"kind": "nope"}, headers=BEARER)
            assert resp.status == 400
            # the endpoint failing is a 502, not a 500
            ep.fail_embed = True
            resp = await client.get(f"/api/search?q=z&cwd={repo}", headers=BEARER)
            assert resp.status == 502
            assert "rag-secret" not in (await resp.text())
        finally:
            await client.close()
            await server.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# what a search costs: the reranker's budget, the query vector, the wait
# --------------------------------------------------------------------------- #
def test_rerank_budget_is_rerank_top_not_the_page_size(tmp_path, repo):
    """``rerank_top`` bounds the reranker; the page size does not widen it.

    The reranker was measured at 18.6 s of a 29.7 s search because a screen
    asking for 30 results sent 30 documents to a reranker configured for 12.
    Results past the budget keep their vector order behind the reranked head.
    """
    ep = Endpoint()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server, rerank_top=2)
            svc = rag.RagService(board=_board(br, repo), config=lambda: cfg,
                                 root_dir=tmp_path / "rag")
            view = await svc.search("beads", "kanban lanes", root=repo, limit=3)
            assert len(view["results"]) == 3
            assert len(ep.rerank_calls[-1]["documents"]) == 2
            assert view["reranked"] is True
            # Only the reranked head carries a rerank score.
            scored = [r for r in view["results"] if "rerank_score" in r]
            assert len(scored) == 2
        finally:
            await server.close()

    asyncio.run(run())


def test_query_vector_is_reused_across_repeats_of_the_same_query(tmp_path, repo):
    """The same query text under the same endpoint and model embeds once.

    Embedding the query was measured at 6.6 s of a 29.7 s search, and a search
    box repeats a query often (a re-opened modal, a filter changed on the
    page, the same question from two sessions).
    """
    ep = Endpoint()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server)
            svc = rag.RagService(board=_board(br, repo), config=lambda: cfg,
                                 root_dir=tmp_path / "rag")
            first = await svc.search("beads", "relay", root=repo, limit=1, rerank=False)
            assert first["timing"]["embed_cached"] is False
            calls = len(ep.embed_calls)
            again = await svc.search("beads", "relay", root=repo, limit=1, rerank=False)
            assert again["timing"]["embed_cached"] is True
            assert len(ep.embed_calls) == calls
            assert again["results"][0]["id"] == first["results"][0]["id"]
            # Another model is another key: the vector is fetched again.
            cfg["embedding_model"] = "emb2"
            svc._indexes.clear()
            third = await svc.search("beads", "relay", root=repo, limit=1, rerank=False)
            assert third["timing"]["embed_cached"] is False
            # The cache holds at most QUERY_CACHE entries, oldest dropped first.
            for n in range(rag.QUERY_CACHE + 5):
                await svc.search("beads", "q%d" % n, root=repo, limit=1, rerank=False)
            assert len(svc._qvecs) == rag.QUERY_CACHE
        finally:
            await server.close()

    asyncio.run(run())


def test_search_waits_for_a_sync_only_while_the_index_is_empty(tmp_path, repo):
    """A filled index answers now; an empty one still waits for its sync.

    The answer reports its own coverage, so spending the wait budget on every
    search buys nothing once there is something to rank.
    """
    ep = Endpoint()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server)
            svc = rag.RagService(board=_board(br, repo), config=lambda: cfg,
                                 root_dir=tmp_path / "rag")
            waits = []
            real_wait = svc.wait_sync

            async def record(prog, budget):
                waits.append(budget)
                await real_wait(prog, budget)

            svc.wait_sync = record
            first = await svc.search("beads", "relay", root=repo, limit=1, rerank=False)
            assert first["results"] and waits == [2.0]
            await svc.search("beads", "kanban", root=repo, limit=1, rerank=False)
            assert waits == [2.0]
        finally:
            await server.close()

    asyncio.run(run())


def test_rank_survives_entries_changing_while_it_scans(tmp_path):
    """``rank`` runs in a worker thread while the sync writes the same dict.

    Iterating the live dict raised "dictionary changed size during iteration"
    and the request answered HTTP 500 (daemon.log, 2026-09-21).
    """
    index = rag.VectorIndex(tmp_path / "i.json", model="emb", dims=DIMS)
    for n in range(200):
        doc = rag.Doc("d%d" % n, "h%d" % n, ["relay"], {"title": "d%d" % n})
        index.put(doc, [rag.unit(_vector_for("relay"))])

    stop = False

    def churn():
        n = 1000
        while not stop:
            doc = rag.Doc("late%d" % n, "h", ["relay"], {"title": "late"})
            index.put(doc, [rag.unit(_vector_for("relay"))])
            index.drop("late%d" % (n - 1))
            n += 1

    writer = threading.Thread(target=churn)
    writer.start()
    try:
        for _ in range(50):
            ranked = index.rank(rag.unit(_vector_for("relay")), 5)
            assert len(ranked) == 5
    finally:
        stop = True
        writer.join()


# --------------------------------------------------------------------------- #
# the CLI
# --------------------------------------------------------------------------- #
def test_cli_search_formats_the_daemon_answer(monkeypatch, capsys):
    from claude_launcher import cli_search

    class Client:
        def __init__(self):
            self.paths = []

        def get(self, path, **kw):
            self.paths.append(path)
            if path.startswith("/api/rag/status"):
                return {"configured": True, "host": "h", "embedding_model": "e", "rerank_model": "",
                        "verify_tls": False, "dimensions": 0,
                        "indexes": [{"kind": "beads", "documents": 3, "indexed": 3, "total": 3,
                                     "pending": 0, "syncing": False, "error": None, "path": "/p"}]}
            return {"kind": "beads", "query": "q", "reranked": True,
                    "index": {"indexed": 2, "total": 3, "syncing": True, "error": None},
                    "results": [{"id": "x-2", "score": 0.5, "rerank_score": 0.91, "title": "pytest",
                                 "status": "closed", "priority": 3, "assignee": "s1", "lexical": True}]}

        def post(self, path, body=None, **kw):
            self.paths.append((path, body))
            return {"kind": "beads", "index": {"indexed": 0, "total": 3, "pending": 3}}

    client = Client()
    monkeypatch.setattr(cli_search, "_client", lambda: client)
    import argparse
    parser = argparse.ArgumentParser()
    cli_search.register(parser.add_subparsers(dest="cmd"))
    args = parser.parse_args(["search", "pytest", "permission", "--limit", "3", "--no-rerank"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "1 result(s)" in out and "2/3 indexed" in out and "sync in progress" in out
    assert "0.910  x-2 (P3, closed, s1)  pytest  [lexical]" in out
    assert "q=pytest+permission" in client.paths[0] and "rerank=0" in client.paths[0]
    assert "limit=3" in client.paths[0] and "cwd=" in client.paths[0]
    args = parser.parse_args(["search", "who", "--kind", "sessions", "--json"])
    assert args.func(args) == 0
    assert json.loads(capsys.readouterr().out)["kind"] == "beads"
    assert "cwd=" not in client.paths[-1]
    args = parser.parse_args(["rag", "status"])
    assert args.func(args) == 0
    out = capsys.readouterr().out
    assert "NO VERIFY" in out and "beads: 3 documents, 3/3 current" in out
    args = parser.parse_args(["rag", "reindex", "--force"])
    assert args.func(args) == 0
    assert client.paths[-1][1]["force"] is True and "cwd" in client.paths[-1][1]
    assert "sync started" in capsys.readouterr().out


def test_cli_search_registers_under_claunch():
    from claude_launcher.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["search", "x"])
    assert args.func.__name__ == "_cmd_search"
    args = parser.parse_args(["rag", "status"])
    assert args.func.__name__ == "_cmd_rag_status"
