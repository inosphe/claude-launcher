"""Unified search corpus and editable endpoint configuration."""
from __future__ import annotations

import asyncio
import hashlib
import json
import math
import os
from urllib.parse import quote, urlsplit
from aiohttp import web
from .. import store, workspaces
from . import beads, rag, search_records


def documents(key, title, text, **meta):
    # Each chunk is a result: long comments and evidence remain searchable.
    chunks = rag.chunk_text(title, text, cap=max(1, len(text) + 1))
    return [rag.Doc(f"{key}:{i}", rag.content_hash(chunk), [chunk],
                    {"title": title, "excerpt": chunk, **meta}) for i, chunk in enumerate(chunks)]


class Corpus:
    def __init__(self, service, observer):
        self.service, self.observer = service, observer
        self.imported = False

    async def docs(self):
        service = self.service
        sessions = service.manager.list() if service.manager else []
        roots = {str(p): p for p in service.known_roots()}
        for cwd in [os.getcwd(), *[w.path for w in workspaces.list_all()]]:
            root = await service.resolve_root(cwd)
            if root:
                roots[str(root)] = root
                if hasattr(service, "watch"):
                    service.watch(root)
        # Each session's board, resolved once: the owner matching below used
        # to resolve every session again for every board.
        session_roots = []
        for session in sessions:
            root = await service.resolve_root(session.sdef.cwd)
            session_roots.append(root)
            if root:
                roots[str(root)] = root
        boards = []
        for root in roots.values():
            issues = await service.board.issues(root)
            # br show accepts multiple IDs and includes comments in one read.
            full = []
            for offset in range(0, len(issues), 40):
                ids = [i["id"] for i in issues[offset:offset + 40]]
                data = await service.board.br(root, ["show", *ids])
                full.extend(data if isinstance(data, list) else [data])
            boards.append((root, full))
        if not self.imported:
            search_records.import_current()
            self.imported = True
        # What the rest of the pass needs from live objects, copied here on
        # the loop: the observer's caches and the sessions' definitions are
        # the loop's, and the build below runs on a worker thread. Each event
        # is copied one level deep so a record the loop updates in place is
        # not serialised half-changed.
        snaps = []
        for session, root in zip(sessions, session_roots):
            name = session.sdef.name
            row = self.observer.load_session(name)
            events = [dict(e) for e in row.get("events", []) + self.observer.reports.rows(name)]
            extra = None
            if hasattr(self.observer, "session_events"):
                extra = [dict(e) for e in self.observer.session_events.rows(session)]
            snaps.append(_Snap(name, root, session.sdef.issue, events, extra, session.sdef.task,
                               getattr(session, "created_at", "") or "",
                               getattr(session.sdef, "identity", ""),
                               getattr(session.sdef, "note", ""), row.get("summary")))
        # Everything from here on is CPU and disk -- matching every session
        # against every issue, a sha1 per chunk of every issue, comment and
        # record, and a SQLite write per session -- and the event loop is the
        # one every terminal's keystrokes wait on. On the loop, one pass over
        # 2000 issues and 735 sessions held it for 20 s without a break
        # (claunch-lol7h).
        return await asyncio.to_thread(_build, boards, snaps)


class _Snap:
    """One registered session as the corpus build reads it."""

    __slots__ = ("name", "root", "issue", "events", "extra", "task", "created_at",
                 "identity", "note", "summary")

    def __init__(self, name, root, issue, events, extra, task, created_at, identity, note, summary):
        self.name, self.root, self.issue = name, root, issue
        self.events, self.extra, self.task = events, extra, task
        self.created_at, self.identity, self.note, self.summary = created_at, identity, note, summary


def _build(boards, snaps):
    """The corpus from what :meth:`Corpus.docs` gathered. Runs on a worker
    thread: it touches no live object, only the copies it was handed and the
    record archive."""
    docs = []
    for root, full in boards:
        owners = {}
        for snap in snaps:
            if snap.root != root:
                continue
            for match in beads.match(full, snap.name, issue=snap.issue, task=snap.task):
                owners.setdefault(match["id"], []).append({"name": snap.name, "via": match["via"]})
        board_key = hashlib.sha256(str(root).encode()).hexdigest()[:16]
        for issue in full:
            iid = issue["id"]
            common = {"kind": "beads", "issue": iid, "root": str(root),
                      "sessions": owners.get(iid, []), "at": issue.get("updated_at"),
                      "href": "#/beads/" + quote(iid, safe=""),
                      "source_url": "api/beads/" + quote(iid, safe="") + "?cwd=" + quote(str(root), safe="")}
            title = iid + " · " + issue.get("title", "")
            text = "\n".join(str(issue.get(k) or "") for k in ("description", "design", "acceptance_criteria", "notes"))
            text += "\n" + " ".join(issue.get("labels") or [])
            docs.extend(documents(f"beads:{board_key}:{iid}", title, text, **common))
            for comment in issue.get("comments", []):
                docs.extend(documents(f"comment:{board_key}:{iid}:{comment['id']}", title + " · comment",
                                      comment.get("text", ""), **{**common, "kind": "comment", "at": comment.get("created_at")}))
    # Every session's records in one transaction, in the order the per-session
    # calls used to make them. The opening task is archived rather than
    # inlined below. Archived, it survives the session leaving the registry,
    # and it comes back in the loop underneath as its own result -- one
    # carrying a source_url, so a reader can open the whole task instead of
    # the passage that matched. Inlining it here as well would put the same
    # text on screen twice for one match.
    batch = []
    for snap in snaps:
        batch.append((snap.name, snap.events))
        if snap.extra is not None:
            batch.append((snap.name, snap.extra))
        task = search_records.task_event(snap.task, snap.created_at)
        if task is not None:
            batch.append((snap.name, [task]))
    # No change hook for these writes: they are read back just below, so this
    # pass already covers them (see search_records.remember_many).
    search_records.remember_many(batch, notify=False)
    for snap in snaps:
        name = snap.name
        # The note is the reader's own word for why this terminal exists,
        # so it belongs in the session's searchable text rather than only
        # on the row: it is how somebody finds the session they annotated
        # a fortnight ago and can no longer name. Read through getattr for
        # the same reason ``identity`` is -- a record written by an older
        # daemon has no such key and must not cost the corpus the session.
        # ``name`` is carried as well as ``sessions``: a result row that
        # *is* a session has to be able to say which one, so that the
        # answer can carry that session's state on the row itself rather
        # than only on a link beside it (see RagService._live_states).
        docs.extend(documents("session:" + name, name, "\n".join(str(v or "") for v in
                              (snap.identity, snap.note, snap.summary)),
                              kind="session", name=name, sessions=[{"name": name}],
                              href="#/s/" + quote(name, safe="")))
    for event in search_records.rows():
        name, eid = event["session"], event["id"]
        text = event.get("text", "")
        if event.get("evidence"):
            text += "\n" + json.dumps(event["evidence"], ensure_ascii=False)
        if event.get("details"):
            text += "\n" + json.dumps(event["details"], ensure_ascii=False)
        if event.get("answer"):
            text += "\n" + event["answer"].get("text", "")
        kind = event.get("kind", "observer")
        # An opening task belongs to the session rather than to its
        # Observer stream, so its result opens the session page the way
        # the session's own document does.
        href = "#/s/" if kind == "opening-task" else "#/observer/session/"
        docs.extend(documents(f"event:{name}:{eid}", f"{name} · {kind}", text,
                              kind=kind, event=eid, at=event.get("at"),
                              sessions=[{"name": name}], href=href + quote(name, safe=""),
                              source_url=f"api/search/records/{quote(name, safe='')}/{quote(eid, safe='')}"))
    return docs


EDITABLE = {"base_url", "api_key", "embedding_model", "rerank_model", "verify_tls", "timeout", "batch", "candidates", "rerank_top", "watch_interval"}


def proposed(body):
    if not isinstance(body, dict) or set(body) - EDITABLE:
        raise ValueError("unsupported search setting")
    doc = store.load()
    block = dict(doc.get("rag") or {})
    for key, value in body.items():
        if key in ("base_url", "api_key", "embedding_model", "rerank_model"):
            if not isinstance(value, str):
                raise ValueError(f"{key} must be text")
            if key == "api_key" and not value:
                continue  # An empty password field preserves the saved secret.
            value = value.strip()
        elif key == "verify_tls":
            if not isinstance(value, bool):
                raise ValueError("verify_tls must be boolean")
        else:
            if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
                raise ValueError(f"{key} must be a number")
            if value < (0 if key == "watch_interval" else 1) or value > 10000:
                raise ValueError(f"{key} is out of range")
            if key in ("batch", "candidates", "rerank_top") and int(value) != value:
                raise ValueError(f"{key} must be an integer")
        block[key] = value
    address = urlsplit(block.get("base_url", ""))
    if address.scheme not in ("http", "https") or not address.hostname or address.username or address.password or address.query or address.fragment:
        raise ValueError("base_url must be an HTTP(S) endpoint without credentials, query or fragment")
    block.pop("dimensions", None)  # Output width is learned from model responses.
    doc["rag"] = block
    return doc


def public_settings():
    cfg = store.rag_config()
    return {**{k: cfg[k] for k in EDITABLE - {"api_key"}}, "api_key_set": bool(cfg["api_key"])}


def install(app):
    service = app["rag"]
    service.all_docs = Corpus(service, app["observer"]).docs
    changed = lambda: service.enqueue("all")
    search_records.change_hooks.append(changed)

    async def cleanup(_app):
        search_records.change_hooks.remove(changed)
    app.on_shutdown.append(cleanup)

    async def settings(request):
        if request.method == "GET":
            return web.json_response(public_settings())
        try:
            doc = proposed(await request.json())
            # Stop old-model syncs before changing their endpoint or vectors.
            await service.shutdown()
            store.save(doc)
            service._indexes.clear()
            service._progress.clear()
            service.start()
            await service.catch_up()
            service.enqueue("all")
            return web.json_response(public_settings())
        except (ValueError, store.StoreError) as exc:
            service.start()
            return web.json_response({"error": str(exc)}, status=400)

    async def test(request):
        stage, dimensions = "embedding", None
        try:
            cfg = store.rag_config(proposed(await request.json()))
            if not store.rag_configured(cfg):
                raise ValueError("API address, key and embedding model are required")
            client = service._client(cfg)
            vectors = await client.embed(["search connection test"])
            dimensions = len(vectors[0])
            if cfg.get("rerank_model"):
                stage = "rerank"
                scores = await client.rerank("search", ["search connection test"], 1)
                if not scores:
                    raise ValueError("rerank returned no results")
            return web.json_response({"ok": True, "dimensions": len(vectors[0]), "rerank": bool(cfg.get("rerank_model"))})
        except (ValueError, rag.RagError, store.StoreError):
            # Endpoint errors can echo credentials or private provider output.
            message = (f"Embedding succeeded ({dimensions} dimensions); rerank failed. Check the rerank model and endpoint."
                       if stage == "rerank" else "Embedding connection test failed. Check endpoint, credentials and model ID.")
            return web.json_response({"error": message, "stage": stage, "dimensions": dimensions}, status=400)

    async def record(request):
        event = search_records.find(request.match_info["name"], request.match_info["event"])
        if event is None:
            raise web.HTTPNotFound()
        return web.json_response(event)

    app.router.add_get("/api/rag/settings", settings)
    app.router.add_put("/api/rag/settings", settings)
    app.router.add_post("/api/rag/test", test)
    app.router.add_get("/api/search/records/{name}/{event}", record)
