"""Semantic search over the board and the fleet: an embedding index plus an
optional reranker, behind ``GET /api/search`` and ``claunch search``.

Two corpora are indexed. The **board** (one index per repository board):
every issue's title, labels and description, chunked so a long write-up
matches on any of its parts. The **fleet** (one index per daemon): every
session's opening task, identity, linked issue and the cached briefing, so
"which session is working on X" is a query rather than a scroll.

The pipeline is retrieve-then-rerank. A query is embedded once, ranked by
cosine against every stored vector (they are unit vectors, so a dot product
is the cosine), the top ``candidates`` are widened with lexical hits (an id
or title containing every query word — the one thing an embedding is worse
at than ``grep``), and the best ``rerank_top`` of those are handed to the
reranker when one is configured. Vector ranking alone answers in one
embeddings call; the reranker costs about a quarter second per document on
the measured endpoint, which is why it scores a dozen and not the board.

Indexes are machine-local derived data under ``<daemon dir>/rag/`` — the same
rule ``briefings.json`` follows: ``~/.claunch.yaml`` carries configuration and
is synced, this directory carries what a reindex can rebuild and is not. Each
entry remembers a content hash, so a sync embeds only what changed since the
last one; a full first index of a 778-issue board was measured at 10-12
minutes and an incremental one at a second or two per changed issue, which is
why syncing is a background task and a search reports how much of the corpus
it covered rather than waiting for all of it.

Configuration is the ``rag:`` block (``store.rag_config``). An empty api key
means the feature is off. The key leaves this module only as the
``Authorization`` header of the endpoint call — never in an error message,
never in status.
"""

from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import math
import re
import sys
import time
from array import array
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, Iterable, List, NamedTuple, Optional, Tuple

import aiohttp

from .. import atomic, store
from . import paths

#: Text handed to the embedder per chunk, in characters. About 1000-1500
#: tokens of mixed Korean and English, well under the endpoint's 40960-token
#: window; small enough that a 30 KB issue is eight chunks, not one vector
#: that averages everything it said.
CHUNK_CHARS = 3000
#: A document contributes at most this many chunks. The board's longest
#: description was 32037 characters; past this cap the tail is a merge log
#: nobody searches for.
MAX_CHUNKS = 8
#: What the reranker reads per candidate: the title and this much of the
#: body. Its cost is per token, and the vector stage already decided the
#: candidate is about the query; the reranker settles the order.
RERANK_CHARS = 600
#: What a search result carries as its excerpt.
EXCERPT_CHARS = 240
#: Index file format version; a file of another version is rebuilt.
FORMAT = 1


class RagError(Exception):
    """The endpoint call failed (transport, HTTP status, or an unusable body)."""


# --------------------------------------------------------------------------- #
# vectors
# --------------------------------------------------------------------------- #
def unit(values: Iterable[float]) -> array:
    """The vector as a unit-length float32 array (a zero vector stays zero)."""
    vec = array("f", values)
    norm = math.sqrt(sum(x * x for x in vec))
    if norm > 0 and abs(norm - 1.0) > 1e-4:
        vec = array("f", (x / norm for x in vec))
    return vec


def dot(a: array, b: array) -> float:
    """Cosine of two unit vectors of the same width."""
    if len(a) != len(b):
        return 0.0
    return sum(map(float.__mul__, a, b))


def _encode(vec: array) -> str:
    out = array("f", vec)
    if sys.byteorder != "little":
        out.byteswap()
    return base64.b64encode(out.tobytes()).decode("ascii")


def _decode(text: str) -> array:
    vec = array("f")
    vec.frombytes(base64.b64decode(text))
    if sys.byteorder != "little":
        vec.byteswap()
    return vec


# --------------------------------------------------------------------------- #
# the endpoint
# --------------------------------------------------------------------------- #
class RagClient:
    """The OpenAI-compatible embeddings call and the Cohere-shaped rerank call.

    ``base_url`` is the ``/v1`` root; ``/embeddings`` and ``/rerank`` hang off
    it (the measured endpoint answers exactly those two; ``/reranking`` is a
    404 there). ``verify_tls`` false hands aiohttp ``ssl=False`` — the
    endpoint this was built against serves a chain Python refuses.
    """

    def __init__(self, cfg: dict) -> None:
        self.cfg = cfg
        self.base = str(cfg.get("base_url") or "").rstrip("/")
        self.timeout = float(cfg.get("timeout") or store.RAG_DEFAULTS["timeout"])
        self.batch = max(1, int(cfg.get("batch") or store.RAG_DEFAULTS["batch"]))

    def _session(self) -> aiohttp.ClientSession:
        headers = {
            "Accept": "application/json",
            "Content-Type": "application/json",
            "Authorization": f"Bearer {self.cfg.get('api_key', '')}",
        }
        connector = aiohttp.TCPConnector(ssl=False) if self.cfg.get("verify_tls") is False else None
        return aiohttp.ClientSession(
            headers=headers,
            connector=connector,
            timeout=aiohttp.ClientTimeout(total=self.timeout),
        )

    async def _post(self, http: aiohttp.ClientSession, path: str, body: dict) -> Any:
        try:
            async with http.post(self.base + path, json=body) as resp:
                if resp.status != 200:
                    snippet = (await resp.text())[:300]
                    raise RagError(f"rag endpoint answered {resp.status} on {path}: {snippet}")
                return await resp.json(content_type=None)
        except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
            raise RagError(f"rag call failed on {path}: {exc}") from exc

    async def embed(self, texts: List[str]) -> List[array]:
        """One unit vector per text, in order; batched by ``batch``."""
        if not texts:
            return []
        out: List[array] = []
        async with self._session() as http:
            for start in range(0, len(texts), self.batch):
                chunk = texts[start:start + self.batch]
                body: dict = {"model": self.cfg["embedding_model"], "input": chunk}
                dims = int(self.cfg.get("dimensions") or 0)
                if dims > 0:
                    body["dimensions"] = dims
                data = await self._post(http, "/embeddings", body)
                try:
                    rows = sorted(data["data"], key=lambda r: int(r.get("index", 0)))
                    vecs = [unit(r["embedding"]) for r in rows]
                except (KeyError, TypeError, ValueError):
                    raise RagError("rag embeddings response has no data[].embedding") from None
                if len(vecs) != len(chunk):
                    raise RagError(
                        f"rag embeddings response carried {len(vecs)} vectors for {len(chunk)} inputs"
                    )
                out.extend(vecs)
        return out

    async def rerank(self, query: str, documents: List[str], top_n: int) -> List[Tuple[int, float]]:
        """``(index, relevance)`` pairs, best first, for the top ``top_n`` documents."""
        if not documents or not self.cfg.get("rerank_model"):
            return []
        body = {
            "model": self.cfg["rerank_model"],
            "query": query,
            "documents": documents,
            "top_n": max(1, min(top_n, len(documents))),
        }
        async with self._session() as http:
            data = await self._post(http, "/rerank", body)
        try:
            rows = data["results"]
            out = [(int(r["index"]), float(r["relevance_score"])) for r in rows]
        except (KeyError, TypeError, ValueError):
            raise RagError("rag rerank response has no results[].relevance_score") from None
        out.sort(key=lambda p: -p[1])
        return out


# --------------------------------------------------------------------------- #
# documents and chunks
# --------------------------------------------------------------------------- #
class Doc(NamedTuple):
    id: str
    hash: str
    chunks: List[str]
    meta: dict


def content_hash(*parts: Optional[str]) -> str:
    h = hashlib.sha1()
    for part in parts:
        h.update((part or "").encode("utf-8", "replace"))
        h.update(b"\0")
    return h.hexdigest()


def chunk_text(head: str, body: str, *, size: int = CHUNK_CHARS, cap: int = MAX_CHUNKS) -> List[str]:
    """Split ``body`` into paragraph-aligned windows, each prefixed by ``head``.

    The head (a title line) rides on every chunk so a chunk from deep in a
    description still says which issue it belongs to. An empty body gives the
    head alone.
    """
    head = (head or "").strip()
    body = (body or "").strip()
    if not body:
        return [head] if head else []
    budget = max(200, size - len(head) - 1)
    chunks: List[str] = []
    current: List[str] = []
    used = 0
    for para in re.split(r"\n\s*\n", body):
        para = para.strip()
        if not para:
            continue
        while len(para) > budget:
            if current:
                chunks.append("\n\n".join(current))
                current, used = [], 0
            chunks.append(para[:budget])
            para = para[budget:]
        if used + len(para) + 2 > budget and current:
            chunks.append("\n\n".join(current))
            current, used = [], 0
        current.append(para)
        used += len(para) + 2
    if current:
        chunks.append("\n\n".join(current))
    chunks = chunks[:cap]
    return [f"{head}\n{c}" if head else c for c in chunks]


def _excerpt(text: Optional[str], limit: int = EXCERPT_CHARS) -> str:
    flat = re.sub(r"\s+", " ", str(text or "")).strip()
    return flat if len(flat) <= limit else flat[:limit - 1].rstrip() + "…"


def issue_doc(issue: dict) -> Doc:
    """A board issue as a document: title + labels on every chunk, the
    description split behind them. The hash covers what the text is built
    from, so a comment added elsewhere does not re-embed the issue."""
    title = str(issue.get("title") or "").strip()
    labels = [str(l) for l in (issue.get("labels") or []) if l]
    head = title
    if labels:
        head = f"{title}\n[{' '.join(labels)}]"
    description = str(issue.get("description") or "")
    return Doc(
        id=str(issue.get("id") or ""),
        hash=content_hash(title, " ".join(labels), description),
        chunks=chunk_text(head, description) or [str(issue.get("id") or "")],
        meta={
            "title": title,
            "status": issue.get("status"),
            "priority": issue.get("priority"),
            "issue_type": issue.get("issue_type"),
            "assignee": issue.get("assignee"),
            "labels": labels,
            "updated_at": issue.get("updated_at"),
            "excerpt": _excerpt(description),
        },
    )


def session_doc(info: dict, briefing: Optional[dict] = None) -> Doc:
    """A session as a document: what it was asked to do, who it is, what
    the briefing says it is doing. ``info`` is the session's definition
    fields; ``briefing`` the cached briefing dict when one exists."""
    name = str(info.get("name") or "")
    task = str(info.get("task") or "").strip()
    identity = str(info.get("identity") or info.get("role") or "").strip()
    issue = str(info.get("issue") or "").strip()
    cwd = str(info.get("cwd") or "").strip()
    brief = briefing if isinstance(briefing, dict) else {}
    lines = [f"session {name}"]
    if identity:
        lines.append(f"identity: {identity}")
    if issue:
        lines.append(f"issue: {issue}")
    if cwd:
        lines.append(f"directory: {cwd}")
    body_parts = []
    if task:
        body_parts.append(f"task:\n{task}")
    for key in ("one-line-job-description", "goal", "now", "progress"):
        value = str(brief.get(key) or "").strip()
        if value:
            body_parts.append(f"{key}: {value}")
    body = "\n\n".join(body_parts)
    head = "\n".join(lines)
    return Doc(
        id=name,
        hash=content_hash(head, body),
        chunks=chunk_text(head, body) or [head],
        meta={
            "name": name,
            "status": info.get("status"),
            "identity": identity,
            "issue": issue,
            "cwd": cwd,
            "one_line": str(brief.get("one-line-job-description") or "").strip(),
            "state": str(brief.get("state") or "").strip(),
            "excerpt": _excerpt(task),
        },
    )


# --------------------------------------------------------------------------- #
# the index
# --------------------------------------------------------------------------- #
class Entry(NamedTuple):
    hash: str
    vecs: List[array]
    meta: dict


class VectorIndex:
    """One corpus's vectors, mirrored to a JSON file.

    Keyed by document id; each entry keeps the content hash it was embedded
    from, one vector per chunk, and the display metadata a result needs, so a
    search answers from the index alone. ``model``/``dims`` are recorded and a
    file made under another model is discarded rather than mixed.
    """

    def __init__(self, path: Path, *, model: str, dims: int) -> None:
        self.path = path
        self.model = model
        self.dims = dims
        self.entries: Dict[str, Entry] = {}
        self.updated_at: Optional[str] = None
        self._loaded = False

    # -- persistence ---------------------------------------------------- #
    def load(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        try:
            data = json.loads(self.path.read_text(encoding="utf-8")) if self.path.is_file() else None
        except (OSError, ValueError):
            data = None
        if not isinstance(data, dict) or data.get("format") != FORMAT:
            return
        file_dims = int(data.get("dims") or 0)
        # dims 0 means "the model's own width": adopt whatever the file was
        # made with. A configured width that differs is another index.
        if data.get("model") != self.model or (self.dims and file_dims != self.dims):
            return
        if not self.dims:
            self.dims = file_dims
        docs = data.get("docs")
        if not isinstance(docs, dict):
            return
        for doc_id, row in docs.items():
            try:
                vecs = [_decode(v) for v in row["v"]]
                if not vecs or any(len(v) != self.dims for v in vecs if self.dims):
                    continue
                self.entries[str(doc_id)] = Entry(str(row["h"]), vecs, dict(row.get("m") or {}))
            except (KeyError, TypeError, ValueError):
                continue
        self.updated_at = data.get("updated_at")

    def save(self) -> None:
        data = {
            "format": FORMAT,
            "model": self.model,
            "dims": self.dims,
            "updated_at": self.updated_at,
            "docs": {
                doc_id: {"h": e.hash, "v": [_encode(v) for v in e.vecs], "m": e.meta}
                for doc_id, e in self.entries.items()
            },
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with atomic.scratch(self.path) as tmp:
                tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
                atomic.replace(tmp, self.path)
        except OSError:
            pass

    # -- maintenance ---------------------------------------------------- #
    def diff(self, docs: List[Doc]) -> Tuple[List[Doc], List[str]]:
        """What a sync must do: documents to (re)embed, ids to drop."""
        wanted = {d.id: d for d in docs if d.id}
        stale = [d for d in wanted.values() if d.id not in self.entries or self.entries[d.id].hash != d.hash]
        gone = [doc_id for doc_id in self.entries if doc_id not in wanted]
        return stale, gone

    def put(self, doc: Doc, vecs: List[array]) -> None:
        if not vecs:
            return
        if not self.dims:
            self.dims = len(vecs[0])
        self.entries[doc.id] = Entry(doc.hash, list(vecs), dict(doc.meta))
        self.updated_at = _now_iso()

    def refresh_meta(self, doc: Doc) -> None:
        """Carry changed display fields (status, assignee) for an entry whose
        text — and so whose vectors — did not change."""
        entry = self.entries.get(doc.id)
        if entry is not None and entry.meta != doc.meta:
            self.entries[doc.id] = Entry(entry.hash, entry.vecs, dict(doc.meta))

    def drop(self, doc_id: str) -> None:
        if doc_id in self.entries:
            del self.entries[doc_id]
            self.updated_at = _now_iso()

    # -- queries -------------------------------------------------------- #
    def rank(self, qvec: array, k: int, *, exclude: Iterable[str] = ()) -> List[Tuple[str, float]]:
        """The ``k`` best documents by their best chunk, best first."""
        skip = set(exclude)
        scored: List[Tuple[str, float]] = []
        for doc_id, entry in self.entries.items():
            if doc_id in skip:
                continue
            best = max((dot(qvec, v) for v in entry.vecs), default=-1.0)
            scored.append((doc_id, best))
        scored.sort(key=lambda p: -p[1])
        return scored[:k]

    def neighbors(self, doc_id: str, k: int) -> List[Tuple[str, float]]:
        """Documents nearest to one already in the index (its first chunk)."""
        entry = self.entries.get(doc_id)
        if entry is None or not entry.vecs:
            return []
        return self.rank(entry.vecs[0], k, exclude=(doc_id,))

    def lexical(self, query: str, k: int) -> List[str]:
        """Ids whose id or title contains every query word — the exact-match
        half a vector stage is weakest at (an issue id, a rare token)."""
        terms = [t for t in query.lower().split() if t]
        if not terms:
            return []
        hits = []
        for doc_id, entry in self.entries.items():
            hay = f"{doc_id} {entry.meta.get('title') or entry.meta.get('name') or ''}".lower()
            if all(t in hay for t in terms):
                hits.append(doc_id)
                if len(hits) >= k:
                    break
        return hits


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# the service
# --------------------------------------------------------------------------- #
class Progress:
    """One corpus's sync state, read by status and by every search answer."""

    def __init__(self) -> None:
        self.total = 0
        self.indexed = 0
        self.pending = 0
        self.task: Optional[asyncio.Task] = None
        self.error: Optional[str] = None
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None

    def view(self) -> dict:
        return {
            "total": self.total,
            "indexed": self.indexed,
            "pending": self.pending,
            "syncing": bool(self.task is not None and not self.task.done()),
            "error": self.error,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
        }


KINDS = ("beads", "sessions")


class RagService:
    """Indexes per corpus and the search over them.

    ``board`` is the daemon's :class:`daemon.beads.Board` (issues per
    repository root), ``manager`` its session registry. ``config`` is read on
    every call so an edit to ``~/.claunch.yaml`` takes effect without a
    restart; ``client_factory`` and ``root_dir`` are the test seams.
    """

    def __init__(
        self,
        *,
        board=None,
        manager=None,
        config: Callable[[], dict] = store.rag_config,
        client_factory: Callable[[dict], RagClient] = RagClient,
        root_dir: Optional[Path] = None,
        briefing_for: Optional[Callable[[str], Optional[dict]]] = None,
    ) -> None:
        self.board = board
        self.manager = manager
        self._config = config
        self._client_factory = client_factory
        self._root_dir = root_dir
        self._briefing_for = briefing_for or _cached_briefing
        self._indexes: Dict[str, VectorIndex] = {}
        self._progress: Dict[str, Progress] = {}

    # -- config --------------------------------------------------------- #
    def config(self) -> dict:
        try:
            return self._config()
        except store.StoreError:
            return dict(store.RAG_DEFAULTS)

    def configured(self) -> bool:
        return store.rag_configured(self.config())

    def _client(self, cfg: dict) -> RagClient:
        return self._client_factory(cfg)

    # -- corpora -------------------------------------------------------- #
    def _key(self, kind: str, root: Optional[Path]) -> str:
        if kind == "beads":
            digest = hashlib.sha1(str(root or "").encode("utf-8", "replace")).hexdigest()[:12]
            return f"beads-{digest}"
        return "sessions"

    def _index(self, kind: str, root: Optional[Path], cfg: dict) -> VectorIndex:
        key = self._key(kind, root)
        model = str(cfg.get("embedding_model") or "")
        dims = int(cfg.get("dimensions") or 0)
        index = self._indexes.get(key)
        if index is None or index.model != model or (dims and index.dims != dims):
            base = self._root_dir if self._root_dir is not None else paths.rag_dir()
            index = VectorIndex(base / f"{key}.json", model=model, dims=dims)
            index.load()
            self._indexes[key] = index
        return index

    def _progress_of(self, key: str) -> Progress:
        prog = self._progress.get(key)
        if prog is None:
            prog = Progress()
            self._progress[key] = prog
        return prog

    async def _docs(self, kind: str, root: Optional[Path]) -> List[Doc]:
        if kind == "beads":
            if self.board is None or root is None:
                return []
            issues = await self.board.issues(root)
            return [issue_doc(i) for i in issues if i.get("id")]
        if kind == "sessions":
            if self.manager is None:
                return []
            docs = []
            for session in self.manager.list():
                sdef = getattr(session, "sdef", None)
                if sdef is None:
                    continue
                info = {
                    "name": getattr(sdef, "name", ""),
                    "task": getattr(sdef, "task", None),
                    "identity": getattr(sdef, "identity", None),
                    "role": getattr(sdef, "role", None),
                    "issue": getattr(sdef, "issue", None),
                    "cwd": getattr(sdef, "cwd", None),
                    "status": _status_of(session),
                }
                docs.append(session_doc(info, self._briefing_for(info["name"])))
            return docs
        raise ValueError(f"unknown corpus {kind!r}")

    async def resolve_root(self, cwd: Optional[str]) -> Optional[Path]:
        if self.board is None or not cwd:
            return None
        root = await self.board.root_for(cwd)
        if root is None or not self.board.has_board(root):
            return None
        return root

    # -- syncing -------------------------------------------------------- #
    def ensure_sync(self, kind: str, root: Optional[Path], *, force: bool = False) -> Progress:
        """Start a sync for the corpus unless one is running; return its progress."""
        cfg = self.config()
        key = self._key(kind, root)
        prog = self._progress_of(key)
        if prog.task is not None and not prog.task.done():
            return prog
        if not store.rag_configured(cfg):
            prog.error = "rag: block not configured"
            return prog
        prog.task = asyncio.create_task(self._sync(kind, root, cfg, prog, force))
        return prog

    async def _sync(self, kind: str, root: Optional[Path], cfg: dict, prog: Progress, force: bool) -> None:
        prog.error = None
        prog.started_at = _now_iso()
        prog.finished_at = None
        try:
            index = self._index(kind, root, cfg)
            docs = await self._docs(kind, root)
            if force:
                index.entries.clear()
            stale, gone = index.diff(docs)
            for doc_id in gone:
                index.drop(doc_id)
            for doc in docs:
                index.refresh_meta(doc)
            prog.total = len(docs)
            prog.pending = len(stale)
            prog.indexed = prog.total - prog.pending
            if not stale:
                if gone:
                    index.save()
                return
            client = self._client(cfg)
            batch = max(1, int(cfg.get("batch") or 1))
            # Embed document by document in groups whose chunk count fits one
            # request, saving after each group so an interrupted sync keeps
            # what it did and the next one starts where it stopped.
            group: List[Doc] = []
            chunks = 0
            for doc in stale:
                if group and chunks + len(doc.chunks) > batch:
                    await self._embed_group(client, index, group, prog)
                    group, chunks = [], 0
                group.append(doc)
                chunks += len(doc.chunks)
            if group:
                await self._embed_group(client, index, group, prog)
        except RagError as exc:
            prog.error = str(exc)
            index = self._indexes.get(self._key(kind, root))
            if index is not None:
                index.save()
        except Exception as exc:  # a corpus read failed; say so, keep the daemon
            prog.error = f"{type(exc).__name__}: {exc}"
        finally:
            prog.finished_at = _now_iso()

    async def _embed_group(self, client: RagClient, index: VectorIndex, group: List[Doc], prog: Progress) -> None:
        texts = [c for d in group for c in d.chunks]
        vecs = await client.embed(texts)
        pos = 0
        for doc in group:
            n = len(doc.chunks)
            index.put(doc, vecs[pos:pos + n])
            pos += n
            prog.indexed += 1
            prog.pending = max(0, prog.pending - 1)
        index.save()

    async def wait_sync(self, prog: Progress, budget: float) -> None:
        """Give a running sync up to ``budget`` seconds to finish."""
        task = prog.task
        if task is None or task.done() or budget <= 0:
            return
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=budget)
        except (asyncio.TimeoutError, asyncio.CancelledError):
            return
        except Exception:
            return

    # -- search --------------------------------------------------------- #
    async def search(
        self,
        kind: str,
        query: str,
        *,
        root: Optional[Path] = None,
        limit: int = 10,
        rerank: bool = True,
        wait: float = 2.0,
    ) -> dict:
        """Rank the corpus for ``query``; the answer names its own coverage."""
        if kind not in KINDS:
            raise ValueError(f"unknown corpus {kind!r}")
        cfg = self.config()
        if not store.rag_configured(cfg):
            raise RagError("rag: block not configured (base_url, api_key, embedding_model)")
        query = (query or "").strip()
        if not query:
            raise ValueError("empty query")
        prog = self.ensure_sync(kind, root)
        await self.wait_sync(prog, wait)
        index = self._index(kind, root, cfg)
        client = self._client(cfg)
        started = time.monotonic()
        qvec = (await client.embed([query]))[0]
        embed_ms = int((time.monotonic() - started) * 1000)
        candidates = max(limit, int(cfg.get("candidates") or limit))
        ranked = await asyncio.to_thread(index.rank, qvec, candidates)
        scores: Dict[str, float] = dict(ranked)
        order = [doc_id for doc_id, _ in ranked]
        lexical = set()
        for doc_id in index.lexical(query, limit):
            lexical.add(doc_id)
            if doc_id not in scores:
                scores[doc_id] = 0.0
                order.insert(0, doc_id)
            else:
                order.remove(doc_id)
                order.insert(0, doc_id)
        reranked = False
        rerank_scores: Dict[str, float] = {}
        rerank_ms = None
        if rerank and cfg.get("rerank_model") and order:
            top = order[: max(limit, int(cfg.get("rerank_top") or limit))]
            texts = [self._rerank_text(index, doc_id) for doc_id in top]
            started = time.monotonic()
            pairs = await client.rerank(query, texts, len(top))
            rerank_ms = int((time.monotonic() - started) * 1000)
            if pairs:
                reranked = True
                for idx, score in pairs:
                    if 0 <= idx < len(top):
                        rerank_scores[top[idx]] = score
                scored_top = sorted(top, key=lambda d: -rerank_scores.get(d, -1.0))
                order = scored_top + [d for d in order if d not in set(top)]
        results = []
        for doc_id in order[:limit]:
            entry = index.entries.get(doc_id)
            if entry is None:
                continue
            row = {
                "id": doc_id,
                "score": round(scores.get(doc_id, 0.0), 4),
                "lexical": doc_id in lexical,
            }
            if doc_id in rerank_scores:
                row["rerank_score"] = round(rerank_scores[doc_id], 4)
            row.update(entry.meta)
            results.append(row)
        return {
            "kind": kind,
            "query": query,
            "root": str(root) if root else None,
            "results": results,
            "reranked": reranked,
            "index": prog.view(),
            "timing": {"embed_ms": embed_ms, "rerank_ms": rerank_ms},
        }

    def _rerank_text(self, index: VectorIndex, doc_id: str) -> str:
        meta = index.entries[doc_id].meta
        title = meta.get("title") or meta.get("name") or doc_id
        excerpt = meta.get("excerpt") or meta.get("one_line") or ""
        return f"{title}\n{excerpt[:RERANK_CHARS]}"

    async def related(self, root: Path, issue_id: str, *, limit: int = 8, wait: float = 2.0) -> dict:
        """The issues nearest to one — the dedup question, asked of the index."""
        cfg = self.config()
        if not store.rag_configured(cfg):
            raise RagError("rag: block not configured (base_url, api_key, embedding_model)")
        prog = self.ensure_sync("beads", root)
        await self.wait_sync(prog, wait)
        index = self._index("beads", root, cfg)
        if issue_id not in index.entries:
            # Not indexed yet (a sync in flight, or a brand-new issue): embed it
            # now from the board rather than answering nothing.
            issues = await self.board.issues(root) if self.board is not None else []
            match = next((i for i in issues if str(i.get("id")) == issue_id), None)
            if match is None:
                return {"id": issue_id, "results": [], "index": prog.view(), "indexed": False}
            doc = issue_doc(match)
            vecs = await self._client(cfg).embed(doc.chunks)
            index.put(doc, vecs)
            index.save()
        pairs = await asyncio.to_thread(index.neighbors, issue_id, limit)
        results = []
        for doc_id, score in pairs:
            entry = index.entries.get(doc_id)
            if entry is None:
                continue
            row = {"id": doc_id, "score": round(score, 4)}
            row.update(entry.meta)
            results.append(row)
        return {"id": issue_id, "results": results, "index": prog.view(), "indexed": True}

    # -- status --------------------------------------------------------- #
    def status(self) -> dict:
        cfg = self.config()
        host = ""
        base = str(cfg.get("base_url") or "")
        m = re.match(r"^[a-z]+://([^/]+)", base)
        if m:
            host = m.group(1)
        indexes = []
        for key, index in self._indexes.items():
            prog = self._progress_of(key)
            row = {
                "key": key,
                "kind": "sessions" if key == "sessions" else "beads",
                "documents": len(index.entries),
                "dims": index.dims,
                "updated_at": index.updated_at,
                "path": str(index.path),
            }
            row.update(prog.view())
            indexes.append(row)
        return {
            "configured": store.rag_configured(cfg),
            "host": host,
            "embedding_model": cfg.get("embedding_model") or "",
            "rerank_model": cfg.get("rerank_model") or "",
            "verify_tls": bool(cfg.get("verify_tls", True)),
            "dimensions": int(cfg.get("dimensions") or 0),
            "indexes": indexes,
        }


def _status_of(session) -> Optional[str]:
    status = getattr(session, "status", None)
    try:
        return status() if callable(status) else status
    except Exception:
        return None


def _cached_briefing(name: str) -> Optional[dict]:
    """The cached briefing dict for a session, without composing one."""
    try:
        from . import briefing
    except Exception:
        return None
    try:
        briefing._restore_cache()
        hit = briefing._cache.get(name)
    except Exception:
        return None
    if not hit:
        return None
    brief = (hit[1] or {}).get("briefing")
    return brief if isinstance(brief, dict) else None
