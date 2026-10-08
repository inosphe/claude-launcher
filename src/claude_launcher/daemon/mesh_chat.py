"""Mesh chat: a room per mesh where the PEOPLE on its daemons talk.

The mesh's message log (:mod:`.mesh`) is the agents' channel — every entry
resolves to member recipients and is typed into their terminals. Chat is the
humans' side of the same mesh and never reaches a terminal: it is its own log,
``chat.jsonl`` beside ``log.jsonl`` in the mesh directory, with its own peer
routes.

Who is talking is the BROWSER, not the daemon token (every browser holding the
token is "the operator"). The page mints a random key into localStorage and
lets the person pick a display name; each message carries both. The key is an
identifier, not a credential — anyone with the daemon token can post as any
key, exactly as anyone with it can type into any terminal.

Federation follows the mesh's own shape (docs/mesh-design.md): the mesh's
authority sequences the room (``seq`` 1, 2, ...) and every other daemon keeps a
mirror in the same order.

* A post on a guest is forwarded to the authority (``/peer/chat/send``); the
  authority's answer is the sequenced entry, which the guest appends.
* The authority pushes every new entry to its linked peers
  (``/peer/chat/sync``) — best-effort, no queue.
* Anything a push missed is pulled: a guest asks the authority for the tail
  after its own length (``/peer/chat/pull``) whenever the page polls and the
  last pull is older than :data:`PULL_INTERVAL`.

Peer calls authenticate with the mesh link tokens, the same pair the mesh's
own peer routes check.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import secrets
import time
from pathlib import Path
from typing import Dict, List, Optional

from aiohttp import web

from .mesh import Mesh, MeshError, MeshManager, PeerUnreachable, utcnow

log = logging.getLogger(__name__)

#: File name of a room's log inside the mesh directory.
CHAT_FILE = "chat.jsonl"
#: Most entries one read (page or peer pull) returns.
PAGE = 500
#: Seconds between two pulls of the same mirrored room.
PULL_INTERVAL = 4.0
MAX_BODY = 4000
MAX_NAME = 40

_KEY_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
_CTRL_RE = re.compile(r"[\x00-\x08\x0b-\x1f\x7f]")


class ChatError(ValueError):
    """A request the room refuses (bad key, empty body, ...): HTTP 400."""


class ChatUnavailable(MeshError):
    """The authority could not be reached to sequence a post: HTTP 503."""


def clean_key(key) -> str:
    key = str(key or "").strip()
    if not _KEY_RE.match(key):
        raise ChatError("chat key must be 8-64 letters, digits, '_' or '-'")
    return key


def clean_name(name) -> str:
    name = _CTRL_RE.sub("", str(name or "")).strip()
    if not name:
        raise ChatError("a display name is required")
    return name[:MAX_NAME]


def clean_body(body) -> str:
    # Newlines and tabs stay: a chat line may be several lines.
    body = _CTRL_RE.sub("", str(body or "").replace("\r\n", "\n")).strip()
    if not body:
        raise ChatError("message is empty")
    if len(body) > MAX_BODY:
        raise ChatError(f"message is longer than {MAX_BODY} characters")
    return body


def _entry(raw) -> Optional[dict]:
    """A stored or received entry, normalised — None when it is not one."""
    if not isinstance(raw, dict):
        return None
    try:
        seq = int(raw.get("seq"))
        return {
            "seq": seq,
            "id": str(raw.get("id") or "")[:64],
            "at": str(raw.get("at") or "")[:40],
            "key": clean_key(raw.get("key")),
            "name": clean_name(raw.get("name")),
            "machine": str(raw.get("machine") or "")[:120],
            "body": clean_body(raw.get("body")),
        }
    except (TypeError, ValueError):
        return None


class Room:
    """One mesh's chat log, in memory and on disk."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self.messages: List[dict] = []
        self.last_pull = 0.0
        self.pulling = False
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        for line in lines:
            try:
                entry = _entry(json.loads(line))
            except ValueError:
                entry = None
            # A torn or out-of-order line ends the readable log: what follows
            # it would carry the wrong seq.
            if entry is None or entry["seq"] != len(self.messages) + 1:
                break
            self.messages.append(entry)

    @property
    def last_seq(self) -> int:
        return len(self.messages)

    def append(self, entry: dict) -> bool:
        """Append ``entry`` if it is the next one; False when it is not."""
        if entry["seq"] != self.last_seq + 1:
            return False
        self.messages.append(entry)
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
        except OSError:
            log.exception("chat: could not write %s", self.path)
        return True

    def after(self, seq: int, limit: int = PAGE) -> List[dict]:
        seq = max(0, int(seq))
        limit = max(1, min(PAGE, int(limit)))
        return self.messages[seq:seq + limit]

    def tail(self, limit: int) -> List[dict]:
        limit = max(1, min(PAGE, int(limit)))
        return self.messages[-limit:]


class ChatManager:
    """Every room this daemon holds — one per mesh it owns or mirrors."""

    def __init__(self, mm: MeshManager) -> None:
        self.mm = mm
        self._rooms: Dict[str, Room] = {}

    # -- rooms ----------------------------------------------------------- #
    def room(self, mesh: Mesh) -> Room:
        path = self.mm._mesh_dir(mesh.name) / CHAT_FILE
        room = self._rooms.get(mesh.name)
        if room is None or room.path != path:
            room = self._rooms[mesh.name] = Room(path)
        return room

    def _summary(self, mesh: Mesh) -> dict:
        room = self.room(mesh)
        last = room.messages[-1] if room.messages else None
        return {
            "mesh": mesh.name,
            "address": self.mm.address(mesh),
            "project": mesh.project,
            "authority": mesh.authority or "",
            "local": not mesh.primary,
            "last_seq": room.last_seq,
            "last": last,
        }

    def rooms(self) -> dict:
        out = []
        for mesh in self.mm.list():
            if mesh.primary:
                self._pull_soon(mesh)
            out.append(self._summary(mesh))
        return {"machine": self.mm.machine or "", "rooms": out}

    def messages(self, ref: str, after: Optional[int] = None,
                 limit: int = 200) -> dict:
        mesh = self.mm.get(ref)
        if mesh.primary:
            self._pull_soon(mesh)
        room = self.room(mesh)
        rows = room.tail(limit) if after is None else room.after(after, limit)
        return {**self._summary(mesh), "messages": rows}

    # -- posting --------------------------------------------------------- #
    def _sequence(self, mesh: Mesh, room: Room, key: str, name: str,
                  body: str, machine: str) -> dict:
        entry = {
            "seq": room.last_seq + 1,
            "id": secrets.token_hex(8),
            "at": utcnow(),
            "key": key,
            "name": name,
            "machine": machine,
            "body": body,
        }
        room.append(entry)
        self._push_soon(mesh, entry)
        return entry

    async def post(self, ref: str, key, name, body) -> dict:
        mesh = self.mm.get(ref)
        key, name, body = clean_key(key), clean_name(name), clean_body(body)
        room = self.room(mesh)
        if not mesh.primary:
            entry = self._sequence(mesh, room, key, name, body,
                                   self.mm.machine or "")
            return {**self._summary(mesh), "message": entry}
        authority = mesh.primary
        link = mesh.links.get(authority)
        if self.mm.peer_transport is None or not link:
            raise ChatUnavailable(
                f"no link to {authority!r}, the daemon that holds this room"
            )
        try:
            resp = await self.mm.peer_transport(authority, "/peer/chat/send", {
                "mesh": mesh.wire_name,
                "machine": self.mm.machine,
                "token": str(link.get("token_out") or ""),
                "message": {"key": key, "name": name, "body": body},
            })
        except PeerUnreachable as exc:
            raise ChatUnavailable(
                f"{authority!r} is unreachable, so the message was not sent: {exc}"
            ) from None
        entry = _entry((resp or {}).get("message"))
        if entry is None:
            raise ChatUnavailable(f"{authority!r} answered without the message")
        if not room.append(entry):
            # We are behind (a push went missing): the pull brings our copy
            # up to and including this entry.
            await self._pull(mesh)
        return {**self._summary(mesh), "message": entry}

    # -- federation: authority side ------------------------------------- #
    def _push_soon(self, mesh: Mesh, entry: dict) -> None:
        if mesh.primary or self.mm.peer_transport is None:
            return
        targets = [m for m in mesh.links if mesh.linked(m)]
        if not targets:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        async def push(machine: str) -> None:
            link = mesh.links.get(machine) or {}
            try:
                await self.mm.peer_transport(machine, "/peer/chat/sync", {
                    "mesh": mesh.wire_name,
                    "machine": self.mm.machine,
                    "token": str(link.get("token_out") or ""),
                    "messages": [entry],
                })
            except Exception as exc:  # noqa: BLE001 — the peer pulls instead
                log.info("chat %r: push to %r failed (%s)", mesh.name, machine, exc)

        for machine in targets:
            asyncio.ensure_future(push(machine))

    def peer_send_accept(self, name: str, machine: str, token: str,
                         message) -> dict:
        mesh = self.mm._inbound(name, machine, token)
        self.mm._check_link_token(mesh, machine, token)
        if mesh.primary:
            raise MeshError(f"this daemon does not hold the room of {name!r}")
        if not isinstance(message, dict):
            raise ChatError("'message' must be an object")
        key = clean_key(message.get("key"))
        nick = clean_name(message.get("name"))
        body = clean_body(message.get("body"))
        entry = self._sequence(mesh, self.room(mesh), key, nick, body, machine)
        return {"message": entry}

    def peer_pull_accept(self, name: str, machine: str, token: str,
                         after) -> dict:
        mesh = self.mm._inbound(name, machine, token)
        self.mm._check_link_token(mesh, machine, token)
        room = self.room(mesh)
        try:
            after = int(after or 0)
        except (TypeError, ValueError):
            after = 0
        return {"messages": room.after(after), "last_seq": room.last_seq}

    # -- federation: mirror side ---------------------------------------- #
    def peer_sync_accept(self, name: str, machine: str, token: str,
                         messages) -> dict:
        mesh = self.mm._inbound(name, machine, token)
        self.mm._check_primary_token(mesh, machine, token)
        room = self.room(mesh)
        for raw in messages if isinstance(messages, list) else []:
            entry = _entry(raw)
            if entry is None or entry["seq"] <= room.last_seq:
                continue
            if not room.append(entry):
                # A gap: the next pull fills it from our true length.
                room.last_pull = 0.0
                return {"resync": room.last_seq}
        return {"last_seq": room.last_seq}

    def _pull_soon(self, mesh: Mesh) -> None:
        room = self.room(mesh)
        if room.pulling or time.monotonic() - room.last_pull < PULL_INTERVAL:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.ensure_future(self._pull(mesh))

    async def _pull(self, mesh: Mesh) -> None:
        """Bring a mirrored room up to the authority's length."""
        authority = mesh.primary
        link = mesh.links.get(authority)
        room = self.room(mesh)
        if not authority or not link or self.mm.peer_transport is None:
            return
        if room.pulling:
            return
        room.pulling = True
        room.last_pull = time.monotonic()
        try:
            for _ in range(20):  # 20 pages = 10000 entries per pull at most
                resp = await self.mm.peer_transport(authority, "/peer/chat/pull", {
                    "mesh": mesh.wire_name,
                    "machine": self.mm.machine,
                    "token": str(link.get("token_out") or ""),
                    "after": room.last_seq,
                })
                rows = (resp or {}).get("messages") or []
                took = 0
                for raw in rows:
                    entry = _entry(raw)
                    if entry is not None and room.append(entry):
                        took += 1
                if not took or room.last_seq >= int((resp or {}).get("last_seq") or 0):
                    break
        except Exception as exc:  # noqa: BLE001 — the next poll tries again
            log.info("chat %r: pull from %r failed (%s)", mesh.name, authority, exc)
        finally:
            room.pulling = False


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #
def _fail(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def install(app: web.Application) -> ChatManager:
    chat = ChatManager(app["mesh"])
    app["mesh_chat"] = chat

    async def body_of(request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeDecodeError):
            body = None
        return body if isinstance(body, dict) else {}

    async def rooms(request):
        return web.json_response(chat.rooms())

    async def messages(request):
        q = request.query
        try:
            after = int(q["after"]) if q.get("after") not in (None, "") else None
            limit = int(q.get("limit") or 200)
        except ValueError:
            return _fail(400, "'after' and 'limit' must be integers")
        try:
            return web.json_response(
                chat.messages(request.match_info["mesh"], after, limit)
            )
        except MeshError as exc:
            return _fail(404, str(exc))

    async def post(request):
        body = await body_of(request)
        try:
            out = await chat.post(request.match_info["mesh"], body.get("key"),
                                  body.get("name"), body.get("body"))
        except ChatError as exc:
            return _fail(400, str(exc))
        except ChatUnavailable as exc:
            return _fail(503, str(exc))
        except MeshError as exc:
            return _fail(404, str(exc))
        return web.json_response(out)

    def peer(handler):
        async def run(request):
            body = await body_of(request)
            try:
                return web.json_response(handler(
                    str(body.get("mesh") or ""), str(body.get("machine") or ""),
                    str(body.get("token") or ""), body,
                ))
            except (ChatError, MeshError) as exc:
                return _fail(400, str(exc))
        return run

    app.router.add_get("/api/chat/rooms", rooms)
    app.router.add_get("/api/chat/rooms/{mesh}/messages", messages)
    app.router.add_post("/api/chat/rooms/{mesh}/messages", post)
    # Called by other daemons over the relay with mesh link tokens; the auth
    # middleware does not gate /peer/*, each handler checks the link token.
    app.router.add_post("/peer/chat/send", peer(
        lambda n, m, t, b: chat.peer_send_accept(n, m, t, b.get("message"))))
    app.router.add_post("/peer/chat/sync", peer(
        lambda n, m, t, b: chat.peer_sync_accept(n, m, t, b.get("messages"))))
    app.router.add_post("/peer/chat/pull", peer(
        lambda n, m, t, b: chat.peer_pull_accept(n, m, t, b.get("after"))))
    return chat
