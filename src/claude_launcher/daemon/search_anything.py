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
        for session in sessions:
            root = await service.resolve_root(session.sdef.cwd)
            if root:
                roots[str(root)] = root
        docs = []
        for root in roots.values():
            issues = await service.board.issues(root)
            # br show accepts multiple IDs and includes comments in one read.
            full = []
            for offset in range(0, len(issues), 40):
                ids = [i["id"] for i in issues[offset:offset + 40]]
                data = await service.board.br(root, ["show", *ids])
                full.extend(data if isinstance(data, list) else [data])
            owners = {}
            for s in sessions:
                if await service.resolve_root(s.sdef.cwd) != root:
                    continue
                for match in beads.match(full, s.sdef.name, issue=s.sdef.issue, task=s.sdef.task):
                    owners.setdefault(match["id"], []).append({"name": s.sdef.name, "via": match["via"]})
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
        if not self.imported:
            search_records.import_current()
            self.imported = True
        for session in sessions:
            name = session.sdef.name
            row = self.observer.load_session(name)
            search_records.remember(name, row.get("events", []) + self.observer.reports.rows(name))
            if hasattr(self.observer, "session_events"):
                search_records.remember(name, self.observer.session_events.rows(session))
            # The note is the reader's own word for why this terminal exists,
            # so it belongs in the session's searchable text rather than only
            # on the row: it is how somebody finds the session they annotated
            # a fortnight ago and can no longer name. Read through getattr for
            # the same reason ``identity`` is -- a record written by an older
            # daemon has no such key and must not cost the corpus the session.
            docs.extend(documents("session:" + name, name, "\n".join(str(v or "") for v in
                                  (session.sdef.task, getattr(session.sdef, "identity", ""),
                                   getattr(session.sdef, "note", ""), row.get("summary"))),
                                  kind="session", sessions=[{"name": name}], href="#/s/" + quote(name, safe="")))
        for event in await asyncio.to_thread(search_records.rows):
            name, eid = event["session"], event["id"]
            text = event.get("text", "")
            if event.get("evidence"):
                text += "\n" + json.dumps(event["evidence"], ensure_ascii=False)
            if event.get("details"):
                text += "\n" + json.dumps(event["details"], ensure_ascii=False)
            if event.get("answer"):
                text += "\n" + event["answer"].get("text", "")
            docs.extend(documents(f"event:{name}:{eid}", f"{name} · {event.get('kind', 'observer')}", text,
                                  kind=event.get("kind", "observer"), event=eid, at=event.get("at"),
                                  sessions=[{"name": name}], href="#/observer/session/" + quote(name, safe=""),
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
