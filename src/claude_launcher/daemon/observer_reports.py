"""Durable agent reports, image evidence and human answers, independent of LLM state."""
from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import uuid
from datetime import datetime, timezone
from aiohttp import web
from .. import atomic

MAX_IMAGE = 5 * 1024 * 1024


def now():
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Reports:
    def __init__(self, root, manager):
        self.root, self.manager = root / "observer-reports", manager
        self.cache = {}
        self.delivering = set()

    def path(self, name):
        return self.root / (hashlib.sha256(name.encode()).hexdigest() + ".json")

    def rows(self, name):
        if name not in self.cache:
            try:
                rows = json.loads(self.path(name).read_text(encoding="utf-8"))
                self.cache[name] = rows if isinstance(rows, list) else []
            except (OSError, ValueError):
                self.cache[name] = []
        return self.cache[name]

    def save(self, name):
        path = self.path(name)
        with atomic.scratch(path) as scratch:
            scratch.write_text(json.dumps(self.rows(name), ensure_ascii=False), encoding="utf-8")
            atomic.replace(scratch, path)

    def session(self, name):
        for session in self.manager.list():
            if session.sdef.name == name:
                return session
        raise web.HTTPNotFound(text="session not found")

    def find(self, name, event_id):
        for event in self.rows(name):
            if event["id"] == event_id:
                return event
        raise web.HTTPNotFound(text="report not found")

    def publish(self, name, body):
        self.session(name)
        if not isinstance(body, dict):
            raise ValueError("object required")
        text = body.get("text")
        if not isinstance(text, str) or not text.strip() or len(text) > 12000:
            raise ValueError("text must contain 1..12000 characters")
        question = body.get("question", False)
        if not isinstance(question, bool):
            raise ValueError("question must be boolean")
        choices = body.get("choices", [])
        if not isinstance(choices, list) or len(choices) > 10 or any(not isinstance(x, str) or not x.strip() or len(x)>300 for x in choices):
            raise ValueError("choices must be at most 10 nonempty strings, each <=300 characters")
        if choices and not question:
            raise ValueError("choices require a question")
        attachments = body.get("attachments", [])
        if not isinstance(attachments, list) or len(attachments)>4:
            raise ValueError("at most 4 attachments")
        image_rows = []
        for image_id in attachments:
            if not isinstance(image_id, str) or len(image_id)!=64 or any(c not in "0123456789abcdef" for c in image_id):
                raise ValueError("invalid attachment id")
            path = self.root / "images" / hashlib.sha256(name.encode()).hexdigest() / image_id
            if not path.is_file():
                raise ValueError("attachment does not belong to this session")
            image_rows.append({"id": image_id})
        state = body.get("state", "working")
        if state not in ("working", "waiting", "blocked", "done", "unknown"):
            raise ValueError("invalid state")
        token = body.get("request_id")
        if token is not None and (not isinstance(token, str) or not token or len(token)>128):
            raise ValueError("request_id must contain 1..128 characters")
        payload = {"text": text, "question": question, "choices": choices, "attachments": image_rows, "state": state}
        for event in self.rows(name):
            if token and event.get("request_id") == token:
                if any(event.get(k)!=v for k,v in payload.items()):
                    raise web.HTTPConflict(text="request_id reused with different content")
                return event
        if sum(e.get("question") and not e.get("answer") for e in self.rows(name))>=200:
            raise ValueError("200 unanswered requests; resolve existing requests first")
        event = {**payload, "id": uuid.uuid4().hex, "request_id": token, "origin": "agent", "source": "agent:"+name,
                 "kind": "action" if question else "result", "needs_action": question, "acknowledged": False, "at": now()}
        rows = self.rows(name)
        rows.append(event)
        # Pending questions remain durable even after the ordinary history limit.
        finished = [e for e in rows if not e.get("question") or e.get("answer")]
        keep = {e["id"] for e in finished[-200:]}
        self.cache[name] = [e for e in rows if e["id"] in keep or (e.get("question") and not e.get("answer"))]
        self.save(name)
        return event

    async def answer(self, name, event_id, body):
        event = self.find(name, event_id)
        if not event.get("question"):
            raise ValueError("report is not a question")
        text = body.get("text") if isinstance(body, dict) else None
        if not isinstance(text, str) or not text.strip() or len(text)>12000:
            raise ValueError("answer must contain 1..12000 characters")
        if event.get("answer"):
            if event["answer"]["text"] != text:
                raise web.HTTPConflict(text="request already answered")
        else:
            event.update(answer={"text": text, "at": now()}, needs_action=False, acknowledged=True, delivery="pending")
            self.save(name)
        if event.get("delivery") in ("sent", "delivering", "unknown") or (name,event_id) in self.delivering:
            return event
        self.delivering.add((name,event_id))
        event["delivery"] = "delivering"
        self.save(name)
        try:
            session = self.session(name)
            if session.exited:
                raise ValueError("session has exited; answer remains available via observer_requests")
            message = "[Observer user answer] " + json.dumps({"request_id":event_id,"question":event["text"],"answer":text},ensure_ascii=False)
            sent = await asyncio.wait_for(session.deliver(message), timeout=20)
            event["delivery"] = "sent" if sent else "pending"
        except Exception:
            # Delivery may already have typed input before failing: never retry
            # an uncertain attempt automatically. The persisted answer is readable.
            event["delivery"] = "unknown"
        finally:
            self.delivering.discard((name,event_id))
            self.save(name)
        return event

    def install(self, app):
        async def publish(request):
            try:
                return web.json_response(self.publish(request.match_info["name"], await request.json()))
            except ValueError as exc:
                return web.json_response({"error":str(exc)},status=400)

        async def requests(request):
            name=request.match_info["name"]
            self.session(name)
            return web.json_response({"reports":self.rows(name)})

        async def answer(request):
            try:
                return web.json_response(await self.answer(request.match_info["name"],request.match_info["event"],await request.json()))
            except ValueError as exc:
                return web.json_response({"error":str(exc)},status=400)

        async def upload(request):
            name=request.match_info["name"]
            self.session(name)
            try:
                body=await request.clone(client_max_size=8*1024*1024).json()
                raw=base64.b64decode(body.get("data", ""),validate=True)
                if not raw or len(raw)>MAX_IMAGE:
                    raise ValueError("image must be <=5 MiB")
                if not (raw.startswith(b"\x89PNG\r\n\x1a\n") or raw.startswith(b"\xff\xd8\xff") or (raw.startswith(b"RIFF") and raw[8:12]==b"WEBP")):
                    raise ValueError("PNG, JPEG or WebP required")
                image_id=hashlib.sha256(raw).hexdigest()
                path=self.root / "images" / hashlib.sha256(name.encode()).hexdigest() / image_id
                with atomic.scratch(path) as scratch:
                    scratch.write_bytes(raw)
                    atomic.replace(scratch,path)
                return web.json_response({"id":image_id})
            except (ValueError, TypeError, AttributeError) as exc:
                return web.json_response({"error":str(exc)},status=400)

        async def image(request):
            event=self.find(request.match_info["name"],request.match_info["event"])
            image_id=request.match_info["image"]
            if image_id not in {a["id"] for a in event.get("attachments",[])}:
                raise web.HTTPNotFound()
            path=self.root / "images" / hashlib.sha256(request.match_info["name"].encode()).hexdigest() / image_id
            raw=path.read_bytes()
            mime="image/png" if raw.startswith(b"\x89PNG") else "image/jpeg" if raw.startswith(b"\xff\xd8") else "image/webp"
            return web.Response(body=raw,content_type=mime,headers={"X-Content-Type-Options":"nosniff","Cache-Control":"private, max-age=86400"})

        app.router.add_post("/api/observer/{name}/reports",publish)
        app.router.add_get("/api/observer/{name}/reports",requests)
        app.router.add_post("/api/observer/{name}/reports/{event}/answer",answer)
        app.router.add_post("/api/observer/{name}/images",upload)
        app.router.add_get("/api/observer/{name}/reports/{event}/images/{image}",image)
