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
        self.rerank_key = "results"

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
        return aioweb.json_response({"id": "rerank-1", self.rerank_key: results[: body.get("top_n", 10)],
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


def test_profile_source_carries_the_provider_and_custom_carries_none():
    """``provider`` is derived for a profile source only.

    It is what decides the request headers the call goes out with
    (:func:`rag.request_headers`); a typed ``base_url`` belongs to no provider
    entry, so nothing is claimed for it.
    """
    from claude_launcher import profile
    profile.create("ds4")

    def mutate(doc):
        doc.setdefault("providers", {})["fw"] = {
            "api_key": "fw-secret",
            "endpoints": {"openai": "https://api.fireworks.ai/inference/v1"},
            "models": {"default": "m"},
        }
        doc.setdefault("profiles", {}).setdefault("ds4", {})["provider"] = "fw"

    store.update(mutate)
    cfg = store.rag_config({**store.load(), "rag": {
        "source": "profile", "profile": "ds4", "embedding_model": "e"}})
    assert cfg["provider"] == "fw"
    assert (cfg["base_url"], cfg["api_key"]) == (
        "https://api.fireworks.ai/inference/v1", "fw-secret")
    typed = store.rag_config({**store.load(), "rag": {
        "base_url": "http://localhost:8000/v1", "api_key": "k",
        "embedding_model": "e"}})
    assert typed["provider"] == ""
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
    path = tmp_path / "idx.zvec"
    index = rag.VectorIndex(path, model="m", dims=0)
    index.load()
    d1 = rag.Doc("a", "h1", ["a one", "a two"], {"title": "A"})
    d2 = rag.Doc("b", "h2", ["b one"], {"title": "B"})
    index.put(d1, [rag.unit([1, 0, 0]), rag.unit([0, 1, 0])])
    index.put(d2, [rag.unit([0, 0, 1])])
    index.save()
    # The collection locks its directory, so the writer lets go before a
    # second index reads the same path back.
    index.close()
    again = rag.VectorIndex(path, model="m", dims=0)
    again.load()
    assert set(again.entries) == {"a", "b"}
    assert again.dims == 3
    assert again.entries["a"].chunks == 2
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
    assert again.lexical("A2", 5) == ["a"]
    again.close()
    # another model's file is not mixed in
    other = rag.VectorIndex(path, model="m2", dims=0)
    other.load()
    assert other.entries == {}
    other.close()


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


def test_client_sends_the_providers_required_session_header():
    """The header leaves on the wire, marked as this feature's own call.

    A provider that refuses a request without its own session header (OpenCode
    Go answers ``400 MissingSessionID``) rejected every sync, because
    ``RagClient._session`` built its headers by hand and never read the
    provider's rule. The indexer calls the upstream directly — no shim in its
    path — so the rule has to be applied here or nowhere.
    """
    ep = Endpoint()

    async def run():
        server = await _start_endpoint(ep)
        try:
            store.update(lambda doc: doc.setdefault("providers", {}).update({
                "ocgo": {"session_header": "x-opencode-session"}}))
            cfg = _cfg(server, provider="ocgo")
            await rag.RagClient(cfg).embed(["kanban"])
            # a typed endpoint names no provider, so no rule applies to it
            await rag.RagClient(_cfg(server, provider="")).embed(["kanban"])
        finally:
            await server.close()

    asyncio.run(run())
    assert ep.headers[0]["x-opencode-session"] == rag.RAG_SESSION_ID
    assert ep.headers[0]["x-opencode-session"] == "claunch-rag"
    assert ep.headers[0]["Authorization"] == "Bearer rag-secret"
    assert "x-opencode-session" not in ep.headers[1]


def test_embeddings_keep_the_bearer_even_when_the_base_url_contains_messages():
    """The Messages-only auth move must not reach this caller at all.

    ``/embeddings`` and ``/rerank`` are the only paths here, so a base URL
    containing ``/messages`` says nothing about the protocol. Passing that URL
    to ``routing.apply_anthropic_auth`` made its substring test the decider:
    an ``anthropic_auth_header: x-api-key`` provider then dropped the bearer
    and left only ``x-api-key`` — a regression against the headers this call
    sent before the session rule existed.
    """
    async def run():
        store.update(lambda doc: doc.setdefault("providers", {}).update({
            "ocgo": {"api_key": "k", "endpoints": {"openai": "https://x/messages/v1"},
                     "session_header": "x-opencode-session",
                     "anthropic_auth_header": "x-api-key"}}))
        cfg = dict(store.RAG_DEFAULTS, base_url="https://x/messages/v1",
                   api_key="k", embedding_model="m", provider="ocgo")
        headers = rag.request_headers(cfg)
        assert headers["Authorization"] == "Bearer k"
        assert "x-api-key" not in headers
        assert headers["x-opencode-session"] == rag.RAG_SESSION_ID

    asyncio.run(run())


def test_a_malformed_rag_header_rule_fails_the_call_with_the_config_key():
    async def run():
        store.update(lambda doc: doc.setdefault("providers", {}).update({
            "ocgo": {"session_header": "x opencode"}}))
        cfg = dict(store.RAG_DEFAULTS, base_url="https://x/v1", api_key="k",
                   embedding_model="m", provider="ocgo")
        with pytest.raises(rag.RagError, match="session_header"):
            async with rag.RagClient(cfg)._session():
                pass

    asyncio.run(run())


def test_client_reads_the_fireworks_rerank_shape():
    """Fireworks puts the rerank rows under ``data`` instead of ``results``."""
    ep = Endpoint()
    ep.rerank_key = "data"

    async def run():
        server = await _start_endpoint(ep)
        try:
            client = rag.RagClient(_cfg(server))
            pairs = await client.rerank("pytest question", ["kanban doc", "pytest doc"], 2)
            assert pairs[0] == (1, pytest.approx(0.9))
            ep.rerank_key = "ranking"
            with pytest.raises(rag.RagError):
                await client.rerank("pytest question", ["kanban doc"], 1)
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
            # The search does not wait on the sync it started: ``pending`` hits 0
            # when the last document is embedded, but the task may still be
            # winding down (``syncing``) when the view is taken -- under load it
            # is. So the view says nothing about ``syncing``; settle, then ask.
            assert view["index"]["pending"] == 0
            await svc.drain(5)
            assert not svc.status()["indexes"][0]["syncing"]
            assert [r["id"] for r in view["results"]] == ["x-2", "x-1"] or \
                [r["id"] for r in view["results"]][0] == "x-2"
            top = view["results"][0]
            assert top["title"] == "pytest basetemp permission error"
            assert top["status"] == "closed" and top["rerank_score"] == pytest.approx(0.9)
            assert view["reranked"] is True
            # documents embedded in one pass: 3 issues, 1 chunk each, + 1 query
            embedded = [t for call in ep.embed_calls for t in call["input"]]
            assert len(embedded) == 4
            # the index exists and a fresh service reads it without re-embedding
            collections = list((tmp_path / "rag").glob("beads-*.zvec"))
            sidecars = list((tmp_path / "rag").glob("beads-*.meta.json"))
            assert len(collections) == 1 and collections[0].is_dir()
            assert len(sidecars) == 1
            before = len(ep.embed_calls)
            # The collection locks its directory and the lock is exclusive --
            # it refuses a read-only open too. So this service lets go before
            # the next one reads the same corpus.
            await svc.shutdown()
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
            assert (tmp_path / "rag" / "sessions.zvec").is_dir()
            assert (tmp_path / "rag" / "sessions.meta.json").is_file()
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
            index = await svc._index("beads", repo, cfg)
            assert "x-4" in index.entries
            # one key never syncs twice at once: the second pass started after the first ended
            assert svc.queue_view()["consumed"] == 2
        finally:
            await svc.shutdown()
            await server.close()

    asyncio.run(run())


def test_a_forced_reindex_during_a_pass_runs_one_more_pass(tmp_path, repo):
    """A Rebuild clicked mid-sync is remembered, not dropped (claunch-i2i95).

    ``ensure_sync`` used to return the running progress and forget the force,
    so the second request left no trace anywhere the reader could see.
    """
    ep = Endpoint()
    br = FakeBr(_issues())

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
            prog = svc.ensure_sync("beads", repo)
            await asyncio.wait_for(reading.wait(), 5)
            again = svc.ensure_sync("beads", repo, force=True)
            assert again is prog
            assert prog.force_pending is True and prog.cancel_requested is False
            gate.set()
            await svc.drain(10)
            # the pass that was running, then the remembered one
            assert prog.runs == 2 and prog.force_pending is False
            assert len(_embedded_docs(ep)) == 6
        finally:
            await svc.shutdown()
            await server.close()

    asyncio.run(run())


def test_a_cancel_stops_the_pass_before_its_next_group_and_keeps_what_it_wrote(tmp_path, repo):
    """Nothing could stop a running pass before this (claunch-i2i95).

    ``pause_backfill`` holds the *next* corpus; the one a pass is on always
    finished. The cancel is cooperative: the group already in flight is not
    torn mid-request, the pass stops at the next boundary, and the vectors it
    wrote stay in the collection.
    """
    class CancelOnFirst(Endpoint):
        def __init__(self):
            super().__init__()
            self.on_first = None

        async def embeddings(self, request):
            response = await super().embeddings(request)
            hook, self.on_first = self.on_first, None
            if hook:
                hook()
            return response

    ep = CancelOnFirst()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server, batch=1)
            svc = rag.RagService(board=_board(br, repo), config=lambda: cfg, root_dir=tmp_path / "rag")
            ep.on_first = lambda: svc.cancel_sync("beads", repo)
            prog = svc.ensure_sync("beads", repo)
            await svc.drain(10)
            assert prog.runs == 1
            assert prog.cancel_requested is True
            # one document reached the endpoint; the group after it did not
            assert prog.indexed == 1 and prog.pending == 2
            index = await svc._index("beads", repo, cfg)
            assert len(index.entries) == 1
            # a corpus with no pass running reports so instead of inventing one
            assert svc.cancel_sync("beads", repo) == {
                "key": svc._key("beads", repo), "running": False, "cancelled": False}
        finally:
            await svc.shutdown()
            await server.close()

    asyncio.run(run())


def test_reindex_returns_whether_it_started_and_cancel_is_a_route(tmp_path, repo, monkeypatch):
    """The card needs an answer to click against (claunch-i2i95).

    ``/api/rag/reindex`` now says what happened to the request — started,
    already running, or queued as a forced pass — and ``/api/rag/cancel``
    is the stop the running pass never had.
    """
    ep = Endpoint()
    br = FakeBr(_issues())
    monkeypatch.chdir(repo)

    async def run():
        server = await _start_endpoint(ep)
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=False)
        board = _board(br, repo)
        cfg_box = {"cfg": _cfg(server)}
        service = rag.RagService(board=board, manager=mgr, config=lambda: cfg_box["cfg"],
                                 root_dir=tmp_path / "rag")
        gate = asyncio.Event()
        reading = asyncio.Event()
        real_docs = service._docs

        async def slow_docs(kind, root):
            reading.set()
            await gate.wait()
            return await real_docs(kind, root)

        service._docs = slow_docs
        client = await _serve(mgr, board, service)
        try:
            resp = await client.post("/api/rag/reindex", json={"cwd": str(repo)}, headers=BEARER)
            assert resp.status == 202
            first = await resp.json()
            assert first["started"] is True and first["force_applied"] is False

            await asyncio.wait_for(reading.wait(), 5)
            resp = await client.post("/api/rag/reindex", json={"cwd": str(repo)}, headers=BEARER)
            during = await resp.json()
            assert during["running"] is True and during["started"] is False
            assert during["force_pending"] is False

            resp = await client.post("/api/rag/reindex",
                                     json={"cwd": str(repo), "force": True}, headers=BEARER)
            forced = await resp.json()
            assert forced["running"] is True and forced["started"] is False
            assert forced["force_pending"] is True

            # the cancel wins over the queued forced pass
            resp = await client.post("/api/rag/cancel", json={"cwd": str(repo)}, headers=BEARER)
            assert resp.status == 200
            cancelled = await resp.json()
            assert cancelled["key"] == service._key("beads", repo)
            assert cancelled["running"] is True and cancelled["cancelled"] is True
            gate.set()
            await service.drain(10)
            prog = service._progress_of(service._key("beads", repo))
            assert prog.runs == 1 and prog.force_pending is False

            resp = await client.post("/api/rag/cancel", json={"cwd": str(repo)}, headers=BEARER)
            assert resp.status == 409
            assert (await resp.json())["cancelled"] is False
            resp = await client.post("/api/rag/cancel", json={"kind": "nope"}, headers=BEARER)
            assert resp.status == 400
        finally:
            await client.close()
            await server.close()

    asyncio.run(run())


def test_status_rows_name_the_collection_db_and_whether_the_path_is_legacy(tmp_path, repo):
    """The two screens judged different evidence (claunch-i2i95).

    The RAG card read the collection's path; Search settings read the
    preset's DB name. A row now carries both, plus whether ``_index_path``
    reached it through the legacy adoption rule.
    """
    br = FakeBr(_issues())

    async def run():
        base = tmp_path / "rag"
        cfg = dict(store.RAG_DEFAULTS)
        cfg.update({"db": "kept", "model_key": "kept", "embedding_model": "emb"})
        svc = rag.RagService(board=_board(br, repo), config=lambda: cfg, root_dir=base)
        await svc._index("beads", repo, cfg)
        row = next(r for r in svc.status()["indexes"] if r["kind"] == "beads")
        assert row["db"] == "kept" and row["db_configured"] == "kept"
        assert row["legacy"] is False

        legacy_cfg = {k: v for k, v in cfg.items() if k not in ("db", "model_key")}
        svc2 = rag.RagService(board=_board(br, repo), config=lambda: legacy_cfg, root_dir=base)
        await svc2._index("beads", repo, legacy_cfg)
        row = next(r for r in svc2.status()["indexes"] if r["kind"] == "beads")
        assert row["db"] == "" and row["db_configured"] == ""
        assert row["legacy"] is True
        await svc.shutdown()
        await svc2.shutdown()

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
            assert "x-9" in (await svc._index("beads", repo, cfg)).entries
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
                assert "s1" in (await svc._index("sessions", None, cfg)).entries
                mgr.sessions.append(Sess("s2"))
                svc.on_sessions_changed(mgr.sessions[-1])  # the exit hook's signature
                await svc.drain()
                assert "s2" in (await svc._index("sessions", None, cfg)).entries
                before = svc._progress_of("sessions").runs
                briefing._persist("s1")           # the briefing cache's hook
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

    The index is settled with ``drain`` before the search, not left to the
    search's two-second wait. That wait is a wall clock over a sync whose
    writes run on the module-global background pool (``rag._sync`` calls
    ``pools.background``); when the sync crosses the budget ``wait_sync``
    returns silently, the search ranks an empty index, and this test fails on
    ``len(results) == 3``. It did so in a full parallel sweep while passing
    every time on its own (claunch-eypmd); why the sync crossed two seconds
    in that sweep is not established. ``drain`` waits for the sync itself, so
    the assertions below measure the reranker budget and not how loaded the
    machine was.
    """
    ep = Endpoint()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server, rerank_top=2)
            svc = rag.RagService(board=_board(br, repo), config=lambda: cfg,
                                 root_dir=tmp_path / "rag")
            svc.ensure_sync("beads", repo)
            await svc.drain(30)
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

    What is measured here is the wait, not what the wait is worth: the budget
    is a wall clock over a sync whose writes run on the module-global
    background pool (``rag._sync`` calls ``pools.background``), so the sync
    can spend the whole two seconds and the search then answers an empty
    index. That answer is legal -- the view reports its own coverage -- so
    asserting results straight after the wait measured the machine's state
    rather than the contract, and it failed that way in a full parallel sweep
    while passing on its own (claunch-eypmd). The index is therefore settled
    with ``drain`` before results are read, and the wait itself is asserted on
    the budgets passed, which is what this test names.
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
            await svc.search("beads", "relay", root=repo, limit=1, rerank=False)
            # The wait is what this test names: an empty index spends it.
            assert waits == [2.0]
            await svc.drain(30)
            filled = await svc.search("beads", "relay", root=repo, limit=1, rerank=False)
            assert filled["results"]
            # A filled index answers from what it holds, so no second wait.
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
    # The daemon accepts three corpora; the CLI refused the third and left the
    # combined one unreachable from a shell (claunch-i2i95.1). It is `unified`
    # and its pre-rename name `all` is gone with the rename (claunch-7aujp.1)
    # — the parser refuses that name, so no request carries it. It is not a
    # board, so no cwd rides with it.
    args = parser.parse_args(["rag", "reindex", "--kind", "unified", "--force"])
    assert args.func(args) == 0
    assert client.paths[-1][1]["kind"] == "unified" and "cwd" not in client.paths[-1][1]
    assert "sync started" in capsys.readouterr().out
    sent = len(client.paths)
    with pytest.raises(SystemExit) as refused:
        parser.parse_args(["rag", "reindex", "--kind", "all", "--force"])
    assert refused.value.code == 2
    assert "invalid choice: 'all'" in capsys.readouterr().err
    assert len(client.paths) == sent, "a refused name must not reach the daemon"
    args = parser.parse_args(["search", "who", "--kind", "unified"])
    assert args.func(args) == 0
    assert "kind=unified" in client.paths[-1] and "cwd=" not in client.paths[-1]
    capsys.readouterr()
    with pytest.raises(SystemExit) as refused:
        parser.parse_args(["search", "who", "--kind", "all"])
    assert refused.value.code == 2
    assert "invalid choice: 'all'" in capsys.readouterr().err
    assert "kind=all" not in client.paths[-1]
    # The daemon takes `mode` (`_search_args`), and `active` only with the
    # combined corpus; the CLI had no way to send either, so the dashboard's
    # active-search was unreachable from a shell (claunch-1uyy1).
    assert "mode=general" in client.paths[-1]
    args = parser.parse_args(["search", "who", "--kind", "unified", "--mode", "active"])
    assert args.func(args) == 0
    assert "kind=unified" in client.paths[-1] and "mode=active" in client.paths[-1]
    assert "cwd=" not in client.paths[-1]
    capsys.readouterr()
    before = len(client.paths)
    args = parser.parse_args(["search", "who", "--mode", "active"])
    with pytest.raises(SystemExit) as exc:
        args.func(args)
    assert exc.value.code == 2
    assert len(client.paths) == before, "a refused pairing must not reach the daemon"
    assert "--mode active is only valid with --kind unified" in capsys.readouterr().err


def test_cli_search_registers_under_claunch():
    from claude_launcher.cli import build_parser

    parser = build_parser()
    args = parser.parse_args(["search", "x"])
    assert args.func.__name__ == "_cmd_search"
    args = parser.parse_args(["rag", "status"])
    assert args.func.__name__ == "_cmd_rag_status"


# --------------------------------------------------------------------------- #
# the collection's own rules
# --------------------------------------------------------------------------- #
def test_vector_ids_are_derived_not_looked_up(tmp_path):
    """The collection's id is a digest of the document id and chunk number.

    zvec accepts only ``[A-Za-z0-9._-]`` in an id and every id in these
    corpora is colon-joined, so the real id cannot be the stored one. Because
    the digest is computed rather than recorded, an update or a delete finds
    the right rows from the document id alone -- no reverse table to keep in
    step, and none to lose.
    """
    a = rag.vector_id("beads:aef8808f7e8d387d:claunch-9sf9", 0)
    assert a == rag.vector_id("beads:aef8808f7e8d387d:claunch-9sf9", 0)
    assert set(a) <= set("0123456789abcdef") and len(a) == 32
    # chunk number and document id both take part
    assert a != rag.vector_id("beads:aef8808f7e8d387d:claunch-9sf9", 1)
    assert a != rag.vector_id("beads:aef8808f7e8d387d:claunch-9sfa", 0)


def test_a_document_that_shrank_leaves_no_chunks_behind(tmp_path):
    """Re-embedding with fewer chunks deletes the tail.

    The ids past the new chunk count are still in the collection, and a
    vector nobody points at still answers a query -- so the document would
    keep matching text it no longer contains.
    """
    index = rag.VectorIndex(tmp_path / "idx.zvec", model="m", dims=3)
    index.put(rag.Doc("a", "h1", [], {"title": "A"}),
              [rag.unit([1, 0, 0]), rag.unit([0, 1, 0]), rag.unit([0, 0, 1])])
    assert index.entries["a"].chunks == 3
    # the middle vector is what the query matches; after the shrink it is gone
    assert index.rank(rag.unit([0, 1, 0]), 5)[0][1] == pytest.approx(1.0)
    index.put(rag.Doc("a", "h2", [], {"title": "A"}), [rag.unit([1, 0, 0])])
    assert index.entries["a"].chunks == 1
    assert index.rank(rag.unit([0, 1, 0]), 5)[0][1] == pytest.approx(0.0, abs=1e-6)
    index.close()


def test_the_index_survives_entries_changing_while_it_ranks(tmp_path):
    """A search runs in a worker thread while the loop's sync writes.

    The JSON index raised "dictionary changed size during iteration" here and
    answered HTTP 500 (fixed in claunch-djfo6). The collection is the one
    doing the scanning now, so the same race has to stay quiet.
    """
    index = rag.VectorIndex(tmp_path / "idx.zvec", model="m", dims=3)
    for i in range(12):
        index.put(rag.Doc(f"d-{i}", f"h{i}", [], {"title": f"t{i}"}),
                  [rag.unit([1, i / 12, 0])])
    errors = []

    def search():
        try:
            for _ in range(20):
                index.rank(rag.unit([1, 0, 0]), 5)
        except Exception as exc:  # noqa: BLE001 -- the failure is the finding
            errors.append(repr(exc))

    def write():
        try:
            for i in range(12, 32):
                index.put(rag.Doc(f"d-{i}", f"h{i}", [], {"title": f"t{i}"}),
                          [rag.unit([0, 1, i / 32])])
        except Exception as exc:  # noqa: BLE001
            errors.append(repr(exc))

    threads = [threading.Thread(target=search), threading.Thread(target=write)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert errors == []
    assert len(index.entries) == 32
    index.close()


def test_a_second_holder_of_one_index_is_named_not_a_traceback(tmp_path):
    """The collection's lock is exclusive -- it refuses read-only opens too.

    So a second holder is a normal situation (a daemon that has not finished
    exiting, a migration run), and what it gets back should say that rather
    than the storage engine's own RuntimeError.
    """
    first = rag.VectorIndex(tmp_path / "idx.zvec", model="m", dims=3)
    first.put(rag.Doc("a", "h1", [], {"title": "A"}), [rag.unit([1, 0, 0])])
    first.save()
    second = rag.VectorIndex(tmp_path / "idx.zvec", model="m", dims=3)
    with pytest.raises(rag.RagError) as caught:
        second.load()
    assert "held by another process" in str(caught.value)
    first.close()
    # once the first lets go, the second reads it
    third = rag.VectorIndex(tmp_path / "idx.zvec", model="m", dims=3)
    third.load()
    assert set(third.entries) == {"a"}
    third.close()


def test_a_sidecar_rewritten_at_another_width_is_named_not_answered_empty(tmp_path):
    """The sidecar describes the vectors beside it, and those were written at
    the width the collection was created with.

    A later model's ``save`` rewrote the sidecar of a collection it had not
    written, so the sidecar claimed a width the collection did not hold and a
    768-dimension query reached a 4096-dimension collection. zvec answered
    with its own "dimension mismatch", and the earlier code answered the same
    disagreement with an empty hit list -- both hidden from the person who
    asked for a search (claunch-rzepk.1).
    """
    path = tmp_path / "idx.zvec"
    built = rag.VectorIndex(path, model="m", dims=3)
    built.put(rag.Doc("a", "h1", [], {"title": "A"}), [rag.unit([1, 0, 0])])
    built.save()
    built.close()
    # another model's save, beside vectors it did not write
    path.with_suffix(".meta.json").write_text(json.dumps({
        "format": rag.FORMAT, "model": "m", "model_key": "k", "dims": 5,
    }))
    index = rag.VectorIndex(path, model="m", dims=5, model_key="k")
    index.load()
    # the width the collection holds, learned on open, not the sidecar's claim
    assert index.index_dims == 3 and index.dims == 5
    with pytest.raises(rag.RagError) as searched:
        index.rank(rag.unit([1, 0, 0, 0, 0]), 5)
    assert "3-dimension" in str(searched.value) and "5" in str(searched.value)
    assert "claunch rag reindex" in str(searched.value)
    assert "index query failed" not in str(searched.value)
    with pytest.raises(rag.RagError) as written:
        index.put(rag.Doc("b", "h2", [], {}), [rag.unit([0, 1, 0, 0, 0])])
    assert str(written.value) == str(searched.value)
    # what save records is the collection's width, so the next reader is told
    # the truth rather than the same wrong number back
    index.save()
    assert json.loads(path.with_suffix(".meta.json").read_text())["dims"] == 3
    index.close()


def test_a_collection_without_a_sidecar_refuses_a_query_of_another_width(tmp_path):
    """With no sidecar the index still knows the collection's width.

    Before, ``self.dims`` was the only width in hand, so a query of the
    configured width went to the engine and came back as zvec's own sentence
    (claunch-rzepk.1).
    """
    path = tmp_path / "idx.zvec"
    built = rag.VectorIndex(path, model="m", dims=3)
    built.put(rag.Doc("a", "h1", [], {}), [rag.unit([1, 0, 0])])
    built.save()
    built.close()
    path.with_suffix(".meta.json").unlink()
    index = rag.VectorIndex(path, model="m", dims=5)
    index.load()
    with pytest.raises(rag.RagError) as caught:
        index.rank(rag.unit([1, 0, 0, 0, 0]), 5)
    assert "holds 3-dimension vectors" in str(caught.value)
    assert "embeds at 5 dimensions" in str(caught.value)
    assert "index query failed" not in str(caught.value)
    # the width is in hand now, so a put of the same width is refused too
    with pytest.raises(rag.RagError):
        index.put(rag.Doc("b", "h2", [], {}), [rag.unit([0, 1, 0, 0, 0])])
    index.close()


def test_a_forced_reindex_clears_the_refusal_it_started_from(tmp_path):
    """``clear`` drops the collection, so it drops the refusal with it.

    Otherwise a reindex -- the very thing the refusal's message tells the
    reader to run -- would be refused by the state it replaced.
    """
    path = tmp_path / "idx.zvec"
    built = rag.VectorIndex(path, model="m", dims=3)
    built.put(rag.Doc("a", "h1", [], {}), [rag.unit([1, 0, 0])])
    built.save()
    built.close()
    path.with_suffix(".meta.json").write_text(json.dumps({
        "format": rag.FORMAT, "model": "m", "model_key": "k", "dims": 5,
    }))
    index = rag.VectorIndex(path, model="m", dims=5, model_key="k")
    index.load()
    assert index.stale_reason
    index.clear()
    assert index.stale_reason == "" and index.index_dims == 0
    index.put(rag.Doc("a", "h1", [], {}), [rag.unit([1, 0, 0, 0, 0])])
    assert index.index_dims == 5 and index.rank(rag.unit([1, 0, 0, 0, 0]), 5)[0][0] == "a"
    index.close()


# --------------------------------------------------------------------------- #
# presets: config resolution, store identity, switching, self test, rate limit
# --------------------------------------------------------------------------- #
def test_rag_config_resolves_the_active_preset():
    cfg = store.rag_config({"rag": {
        "preset": "local",
        "presets": [
            {"id": "local", "source": "custom", "base_url": "http://h/v1",
             "api_key": "k", "embedding_model": "Qwen/Qwen3-Embedding-8B"},
            {"id": "cloud", "source": "custom", "base_url": "http://other/v1",
             "api_key": "k2", "embedding_model": "qwen3-embedding-8b-alias",
             "model_key": "qwen-qwen3-embedding-8b"},
        ],
        "source": "custom",
        "sources": {"custom": {"base_url": "http://legacy/v1", "api_key": "kl",
                               "embedding_model": "legacy-emb", "rerank_model": ""}},
    }})
    assert cfg["preset"] == "local"
    # the preset's connection is in force, the legacy rows are not
    assert (cfg["base_url"], cfg["api_key"], cfg["embedding_model"]) == (
        "http://h/v1", "k", "Qwen/Qwen3-Embedding-8B")
    # no explicit model_key: derived from the model name
    assert cfg["model_key"] == "qwen-qwen3-embedding-8b"


def test_rag_config_explicit_model_key_wins_and_aliases_share_a_store():
    """Two presets naming one real model through different providers or
    aliases share a store by sharing one model_key."""
    doc = {"rag": {"preset": "cloud", "presets": [
        {"id": "local", "source": "custom", "base_url": "http://h/v1",
         "api_key": "k", "embedding_model": "Qwen/Qwen3-Embedding-8B"},
        {"id": "cloud", "source": "custom", "base_url": "http://other/v1",
         "api_key": "k2", "embedding_model": "qwen3-embedding-8b-alias",
         "model_key": "qwen-qwen3-embedding-8b"},
    ]}}
    local = store.rag_config({**doc, "rag": {**doc["rag"], "preset": "local"}})
    cloud = store.rag_config(doc)
    assert cloud["model_key"] == "qwen-qwen3-embedding-8b"
    # derived-from-name and explicit agree: switching between these presets
    # is the same store, whatever the endpoint or alias says
    assert local["model_key"] == cloud["model_key"]


def test_rag_config_dangling_preset_falls_back_to_the_legacy_rows():
    doc = {"rag": {
        "preset": "gone",
        "presets": [{"id": "kept", "source": "custom", "base_url": "http://h/v1",
                     "api_key": "k", "embedding_model": "emb"}],
        "source": "custom",
        "sources": {"custom": {"base_url": "http://legacy/v1", "api_key": "kl",
                               "embedding_model": "legacy-emb", "rerank_model": ""}},
    }}
    cfg = store.rag_config(doc)
    assert cfg["preset"] == ""
    assert cfg["base_url"] == "http://legacy/v1"
    assert cfg["model_key"] == ""  # legacy mode keeps the pre-preset paths


def test_rag_config_rate_limit_coerces_and_zero_is_unlimited():
    assert store.rag_config({})["rate_limit"] == 120
    assert store.rag_config({"rag": {"rate_limit": 0}})["rate_limit"] == 0
    assert store.rag_config({"rag": {"rate_limit": "30"}})["rate_limit"] == 30
    assert store.rag_config({"rag": {"rate_limit": "nope"}})["rate_limit"] == 120
    assert store.rag_config({"rag": {"rate_limit": -5}})["rate_limit"] == 0


def test_rag_model_key_slug():
    slug = store.rag_model_key_slug
    assert slug("Qwen/Qwen3-Embedding-8B") == "qwen-qwen3-embedding-8b"
    assert slug("bge-m3") == "bge-m3"
    assert slug("Jina Embeddings v4 (prod)") == "jina-embeddings-v4-prod"
    assert slug("  Mixed CASE  tokens ") == "mixed-case-tokens"
    assert slug("") == ""
    assert slug("한글 모델") == ""
    assert len(slug("x" * 100)) == store.RAG_MODEL_KEY_MAX


def test_clean_rag_preset_drops_unusable_rows():
    assert store.clean_rag_preset("nope") is None
    assert store.clean_rag_preset({"id": "Bad Name"}) is None
    assert store.clean_rag_preset({}) is None
    # source is case-insensitive; only "profile" is meaningful, anything
    # else reads as the custom connection and is dropped from the row
    assert store.clean_rag_preset({"id": "ok", "source": "PROFILE"}) == {"id": "ok", "source": "profile"}
    assert store.clean_rag_preset({"id": "ok", "source": "profile", "unknown": "x"}) == \
        {"id": "ok", "source": "profile"}
    assert store.clean_rag_preset({"id": "ok", "source": "banana"}) == {"id": "ok"}
    kept = store.rag_presets({"rag": {"presets": [
        {"id": "one", "embedding_model": "e"}, {"id": "one", "embedding_model": "e2"},
        {"id": "Bad"}, {"id": "two"},
    ]}})
    assert [p["id"] for p in kept] == ["one", "two"]
    assert kept[0]["embedding_model"] == "e"  # the first row wins a duplicate id


def test_index_path_uses_the_model_key_and_adopts_the_legacy_collection(tmp_path):
    svc = rag.RagService(config=lambda: dict(store.RAG_DEFAULTS), root_dir=tmp_path)
    base = tmp_path
    # no preset: the pre-preset path, whatever the model
    assert svc._index_path(base, "sessions", {"model_key": ""}) == base / "sessions.zvec"
    cfg = {"model_key": "qwen-qwen3-embedding-8b", "dimensions": 0}
    # nothing on disk yet: a fresh collection under the key
    assert svc._index_path(base, "sessions", cfg) == base / "sessions.qwen-qwen3-embedding-8b.zvec"
    (base / "sessions.qwen-qwen3-embedding-8b.zvec").mkdir(parents=True)
    assert svc._index_path(base, "sessions", cfg) == base / "sessions.qwen-qwen3-embedding-8b.zvec"
    # only the pre-preset collection exists and it is the same identity:
    # adopt it instead of embedding the corpus a second time
    (base / "sessions.qwen-qwen3-embedding-8b.zvec").rmdir()
    (base / "sessions.zvec").mkdir(parents=True)
    (base / "sessions.meta.json").write_text(json.dumps({
        "format": rag.FORMAT, "model": "Qwen/Qwen3-Embedding-8B", "dims": 8,
    }))
    assert svc._index_path(base, "sessions", cfg) == base / "sessions.zvec"
    # a different configured width is another index: no adoption
    assert svc._index_path(base, "sessions", {"model_key": "qwen-qwen3-embedding-8b",
                                              "dimensions": 4}) == \
        base / "sessions.qwen-qwen3-embedding-8b.zvec"
    # a legacy collection of another model is not adopted either
    (base / "sessions.meta.json").write_text(json.dumps({
        "format": rag.FORMAT, "model": "other-model", "dims": 8,
    }))
    assert svc._index_path(base, "sessions", cfg) == base / "sessions.qwen-qwen3-embedding-8b.zvec"


def test_the_combined_corpus_reads_the_pre_rename_all_directory(tmp_path):
    """`all` was the combined corpus's name on disk (claunch-7aujp.1).

    The directories are moved in a window the operator opens, so until then
    the collection is still `all.zvec`: opening it keeps the vectors that
    exist instead of starting a second, empty collection under the new name.
    """
    svc = rag.RagService(config=lambda: dict(store.RAG_DEFAULTS), root_dir=tmp_path)
    base = tmp_path
    cfg = {"model_key": "", "dimensions": 0}
    # nothing on disk: a first write creates the current name
    assert svc._index_path(base, "unified", cfg) == base / "unified.zvec"
    (base / "all.zvec").mkdir(parents=True)
    (base / "all.meta.json").write_text(json.dumps({
        "format": rag.FORMAT, "model": "m", "dims": 8,
    }))
    assert svc._index_path(base, "unified", cfg) == base / "all.zvec"
    # once the directory is moved the new name is the one read
    (base / "unified.zvec").mkdir()
    assert svc._index_path(base, "unified", cfg) == base / "unified.zvec"
    # the adoption is the combined corpus's alone: a board collection named
    # `all-...` is not a thing, and `sessions` has no pre-rename name
    assert svc._index_path(base, "sessions", cfg) == base / "sessions.zvec"


def test_a_preset_collection_reads_the_pre_rename_all_directory(tmp_path):
    svc = rag.RagService(config=lambda: dict(store.RAG_DEFAULTS), root_dir=tmp_path)
    base = tmp_path
    cfg = {"model_key": "gemma-2", "dimensions": 8}
    (base / "all.gemma-2.zvec").mkdir(parents=True)
    assert svc._index_path(base, "unified", cfg) == base / "all.gemma-2.zvec"
    (base / "unified.gemma-2.zvec").mkdir()
    assert svc._index_path(base, "unified", cfg) == base / "unified.gemma-2.zvec"


def test_the_combined_corpus_is_keyed_and_read_by_its_current_name(tmp_path):
    svc = rag.RagService(config=lambda: dict(store.RAG_DEFAULTS), root_dir=tmp_path)
    # the key is the corpus's own name now — the storage pin is gone
    assert svc._key("unified", None) == "unified"
    assert svc._key("sessions", None) == "sessions"
    assert svc._key("beads", tmp_path).startswith("beads-")
    # a collection opened under the pre-rename name still reads as unified
    assert rag.RagService._kind_of_key("all") == "unified"
    assert rag.RagService._kind_of_key("unified") == "unified"
    assert rag.RagService._kind_of_key("sessions") == "sessions"
    assert rag.RagService._kind_of_key("beads-abc123") == "beads"


def test_rag_stores_reads_the_corpus_from_the_directory_name(tmp_path):
    """The DB panel's `corpus` column follows the directory name.

    Both names are on disk on an installation that has not moved the combined
    collection yet, and each row says which one it read.
    """
    (tmp_path / "all.zvec").mkdir()
    (tmp_path / "unified.zvec").mkdir()
    rows = {row["name"]: row for row in rag.rag_stores(tmp_path)}
    assert rows["all"]["corpus"] == "all"
    assert rows["unified"]["corpus"] == "unified"


def test_rate_limiter_paces_requests(monkeypatch):
    async def run():
        monkeypatch.setattr(rag._RateLimiter, "WINDOW", 0.3)
        limiter = rag._RateLimiter()
        start = time.monotonic()
        await limiter.acquire(2, 2)
        await limiter.acquire(2, 2)  # over the cap: waits for the window to slide
        elapsed = time.monotonic() - start
        assert elapsed >= 0.25
        assert limiter.view()["recent"] == 2
        # a limit of 0 never waits
        before = time.monotonic()
        await limiter.acquire(50, 0)
        assert time.monotonic() - before < 0.05

    asyncio.run(run())


def test_rate_limiter_slices_groups_larger_than_the_cap(monkeypatch):
    """A single group with more requests than the cap must not crash:
    the window can never hold n > limit stamps, and the wait-index math
    indexed past the list. The group is admitted in cap-sized slices."""

    async def run():
        monkeypatch.setattr(rag._RateLimiter, "WINDOW", 0.3)
        limiter = rag._RateLimiter()
        start = time.monotonic()
        await limiter.acquire(5, 3)  # used to raise IndexError
        first = time.monotonic() - start
        assert first >= 0.25  # slice 2 had to wait for slice 1 to slide
        assert limiter.view()["recent"] == 2  # 5 = slices of 3 + 2; the 2-stamp slice remains
        # the window is now full (2 + 3 > 3): the next admission paces
        start = time.monotonic()
        await limiter.acquire(3, 3)
        assert time.monotonic() - start >= 0.2

    asyncio.run(run())


def test_presets_switch_stores_by_model_key_and_reuse_vectors(tmp_path, repo):
    """The switch the feature exists for: two aliases of one real model keep
    one collection (no re-embedding); a different model opens its own store
    and backfills; switching back reuses what is on disk."""
    ep = Endpoint()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            holder: dict = {}

            def cfg():
                c = _cfg(server)
                c.update(holder)
                return c

            svc = rag.RagService(board=_board(br, repo), config=cfg, root_dir=tmp_path / "rag")
            key = svc._key("beads", repo)
            base = tmp_path / "rag"
            holder.update({"preset": "one", "model_key": "qwen-qwen3-embedding-8b",
                           "embedding_model": "emb-a"})
            prog = svc.ensure_sync("beads", repo)
            await svc.wait_sync(prog, 5)
            assert prog.error is None
            assert (base / f"{key}.qwen-qwen3-embedding-8b.zvec").is_dir()
            record = json.loads((base / "active.json").read_text())
            assert record["preset"] == "one" and record["model_key"] == "qwen-qwen3-embedding-8b"
            assert record["embedding_model"] == "emb-a"
            assert svc.status()["active"]["preset"] == "one"
            embeds_after_first = len(ep.embed_calls)

            # the alias preset: same store, no re-embed, the record moves on
            holder.update({"preset": "two", "model_key": "qwen-qwen3-embedding-8b",
                           "embedding_model": "emb-b"})
            prog = svc.ensure_sync("beads", repo)
            await svc.wait_sync(prog, 5)
            assert prog.error is None and prog.pending == 0
            assert len(ep.embed_calls) == embeds_after_first  # nothing re-embedded
            assert not (base / f"{key}.emb-b.zvec").exists()
            record = json.loads((base / "active.json").read_text())
            assert record["preset"] == "two" and record["embedding_model"] == "emb-b"
            recorded_at = record["recorded_at"]
            # the same selection does not rewrite the record
            svc._record_active(cfg())
            assert json.loads((base / "active.json").read_text())["recorded_at"] == recorded_at

            # a genuinely different model: its own collection, a backfill
            holder.update({"preset": "three", "model_key": "bge-m3",
                           "embedding_model": "emb-c"})
            prog = svc.ensure_sync("beads", repo)
            await svc.wait_sync(prog, 5)
            assert prog.error is None and prog.indexed == 3  # the corpus, again
            assert (base / f"{key}.bge-m3.zvec").is_dir()
            assert len(ep.embed_calls) > embeds_after_first

            # back to the first store: what it held is reused
            embeds_before_return = len(ep.embed_calls)
            holder.update({"preset": "one", "model_key": "qwen-qwen3-embedding-8b",
                           "embedding_model": "emb-a"})
            prog = svc.ensure_sync("beads", repo)
            await svc.wait_sync(prog, 5)
            assert prog.error is None and prog.pending == 0
            assert len(ep.embed_calls) == embeds_before_return
            assert svc.status()["active"]["model_key"] == "qwen-qwen3-embedding-8b"
            await svc.shutdown()
        finally:
            await server.close()

    asyncio.run(run())


def test_a_preset_with_a_derived_key_adopts_the_legacy_collection(tmp_path, repo):
    """The migration case: an index built before presets keeps serving when
    a preset names the same model -- same identity, no second copy."""
    ep = Endpoint()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            legacy = _cfg(server, embedding_model="Qwen/Qwen3-Embedding-8B")
            legacy["preset"] = ""
            legacy["model_key"] = ""
            svc = rag.RagService(board=_board(br, repo), config=lambda: legacy,
                                 root_dir=tmp_path / "rag")
            prog = svc.ensure_sync("beads", repo)
            await svc.wait_sync(prog, 5)
            key = svc._key("beads", repo)
            base = tmp_path / "rag"
            assert (base / f"{key}.zvec").is_dir()  # the pre-preset path
            before = len(ep.embed_calls)
            await svc.shutdown()

            preset = _cfg(server, embedding_model="qwen3-embedding-8b-alias")
            preset.update({"preset": "cloud",
                           "model_key": "qwen-qwen3-embedding-8b"})
            svc2 = rag.RagService(board=_board(br, repo), config=lambda: preset,
                                  root_dir=tmp_path / "rag")
            prog = svc2.ensure_sync("beads", repo)
            await svc2.wait_sync(prog, 5)
            assert prog.error is None and prog.pending == 0  # adopted, not re-embedded
            assert len(ep.embed_calls) == before
            assert not list(base.glob(f"{key}.*.zvec"))  # no second collection
            await svc2.shutdown()
        finally:
            await server.close()

    asyncio.run(run())


def test_self_test_scores_sampled_entries_against_the_threshold(tmp_path, repo, monkeypatch):
    ep = Endpoint()
    br = FakeBr(_issues())

    async def run():
        server = await _start_endpoint(ep)
        try:
            cfg = _cfg(server, preset="one", model_key="qwen-qwen3-embedding-8b")
            svc = rag.RagService(board=_board(br, repo), config=lambda: cfg,
                                 root_dir=tmp_path / "rag")
            prog = svc.ensure_sync("beads", repo)
            await svc.wait_sync(prog, 5)
            assert prog.error is None

            result = await svc.self_test("beads", repo, sample=3, threshold=0.5, top_k=2)
            assert result["sampled"] == 3 and result["passed"] == 3 and result["failed"] == 0
            assert result["similarity"]["max"] == pytest.approx(1.0)
            assert result["similarity"]["min"] == pytest.approx(1.0)
            assert result["model_key"] == "qwen-qwen3-embedding-8b"
            assert result["preset"] == "one"
            assert result["failures"] == []

            # a low score instead of a confident self-hit: the sample fails
            # and the report carries the score the index actually gave
            index = await svc._index("beads", repo, cfg)
            monkeypatch.setattr(index, "rank", lambda *a, **k: [("x-1", 0.2)])
            result = await svc.self_test("beads", repo, sample=1, threshold=0.5, top_k=2)
            assert result["passed"] == 0 and result["failed"] == 1
            assert result["failures"][0]["score"] == pytest.approx(0.2)
            assert result["failures"][0]["top"][0]["id"] == "x-1"

            # nothing retrieves at all: the failure says "not in the top-k"
            monkeypatch.setattr(index, "rank", lambda *a, **k: [])
            result = await svc.self_test("beads", repo, sample=2, threshold=0.5, top_k=2)
            assert result["passed"] == 0 and result["failed"] == 2
            assert all(f["score"] is None for f in result["failures"])
            await svc.shutdown()
        finally:
            await server.close()

    asyncio.run(run())


def test_self_test_refuses_when_unconfigured_or_empty(tmp_path, repo):
    async def run():
        off = rag.RagService(board=None, config=lambda: dict(store.RAG_DEFAULTS),
                             root_dir=tmp_path / "rag")
        with pytest.raises(rag.RagError):
            await off.self_test("sessions")

        cfg = _cfg_for_selftest()
        svc = rag.RagService(board=None, manager=None, config=lambda: cfg,
                             root_dir=tmp_path / "rag")
        with pytest.raises(rag.RagError):
            await svc.self_test("sessions")  # no registry: nothing to sample
        with pytest.raises(ValueError):
            await svc.self_test("nope")
        with pytest.raises(ValueError):
            await svc.self_test("sessions", threshold=2.0)

    asyncio.run(run())


def _cfg_for_selftest() -> dict:
    cfg = dict(store.RAG_DEFAULTS)
    cfg.update({"base_url": "http://unused/v1", "api_key": "k", "embedding_model": "e"})
    return cfg
