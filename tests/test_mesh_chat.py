"""Mesh chat (daemon/mesh_chat.py): the people's room beside each mesh.

C1 a post on the authority is sequenced, persisted to chat.jsonl and read
   back by a fresh manager; it never enters the mesh's agent log
C2 bad keys, empty names and empty bodies are refused
C3 a guest's post is sequenced by the authority and lands in both rooms;
   the authority's own posts are pushed to the guest
C4 a guest that missed pushes catches up by pulling
C5 a guest whose authority is unreachable refuses the post (nothing stored)
C6 peer calls with a wrong link token are refused
C7 a real federated join (test_mesh_join helpers) carries chat both ways
C8 the HTTP routes: rooms, messages, post, 400 and 404
"""

from __future__ import annotations

import asyncio
import json

import pytest

from claude_launcher.daemon import mesh_chat
from claude_launcher.daemon.mesh import Mesh, MeshError, MeshManager, PeerUnreachable
from claude_launcher.daemon.mesh_chat import ChatError, ChatManager, ChatUnavailable

KEY_A = "browserAAAA1"
KEY_B = "browserBBBB2"


def _chat_dispatch(chats: dict):
    async def call(machine, path, body):
        chat = chats[machine]
        args = (body["mesh"], body["machine"], body.get("token") or "")
        if path == "/peer/chat/send":
            return chat.peer_send_accept(*args, body.get("message"))
        if path == "/peer/chat/sync":
            return chat.peer_sync_accept(*args, body.get("messages"))
        if path == "/peer/chat/pull":
            return chat.peer_pull_accept(*args, body.get("after"))
        raise AssertionError(f"unexpected peer call {path}")
    return call


def _pair(tmp_path):
    """pcA holds mesh 'm'; pcB mirrors it as 'm@pcA' — links set by hand."""
    mm_a = MeshManager(None, root=tmp_path / "A")
    mm_b = MeshManager(None, root=tmp_path / "B")
    mm_a._machine, mm_b._machine = "pcA", "pcB"
    a = mm_a.create("m")
    a.me = "pcA"
    a.peers = ["pcA", "pcB"]
    a.links = {"pcB": {"token_in": "b-to-a", "token_out": "a-to-b"}}
    b = Mesh("m", origin="pcA", me="pcB")
    b.peers = ["pcA", "pcB"]
    b.links = {"pcA": {"token_in": "a-to-b", "token_out": "b-to-a"}}
    mm_b._meshes[b.name] = b
    chats = {"pcA": ChatManager(mm_a), "pcB": ChatManager(mm_b)}
    call = _chat_dispatch(chats)
    mm_a.peer_transport = mm_b.peer_transport = call
    return mm_a, mm_b, chats["pcA"], chats["pcB"]


async def _settle():
    for _ in range(5):
        await asyncio.sleep(0)


def test_c1_authority_post_persists_and_stays_out_of_agent_log(home, tmp_path):
    async def run():
        mm = MeshManager(None, root=tmp_path / "A")
        mesh = mm.create("m")
        chat = ChatManager(mm)
        out = await chat.post("m", KEY_A, " Alice ", "hello\r\nworld")
        assert out["message"]["seq"] == 1
        assert out["message"]["name"] == "Alice"
        assert out["message"]["body"] == "hello\nworld"
        await chat.post("m", KEY_B, "Bob", "hi")
        assert mesh.messages == []  # no agent ever sees chat
        path = tmp_path / "A" / "m" / "chat.jsonl"
        lines = [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines()]
        assert [x["seq"] for x in lines] == [1, 2]
        again = ChatManager(mm)
        got = again.messages("m")
        assert [m["body"] for m in got["messages"]] == ["hello\nworld", "hi"]
        assert again.messages("m", after=1)["messages"][0]["name"] == "Bob"
        rooms = again.rooms()["rooms"]
        assert rooms[0]["mesh"] == "m" and rooms[0]["last_seq"] == 2
        assert rooms[0]["last"]["body"] == "hi" and rooms[0]["local"] is True

    asyncio.run(run())


def test_c1_torn_line_ends_the_readable_log(home, tmp_path):
    mm = MeshManager(None, root=tmp_path / "A")
    mm.create("m")
    path = tmp_path / "A" / "m" / "chat.jsonl"
    good = {"seq": 1, "id": "x", "at": "t", "key": KEY_A, "name": "a",
            "machine": "", "body": "one"}
    path.write_text(json.dumps(good) + "\n{torn\n" + json.dumps({**good, "seq": 3}) + "\n",
                    encoding="utf-8")
    assert ChatManager(mm).messages("m")["last_seq"] == 1


def test_c2_validation(home, tmp_path):
    async def run():
        mm = MeshManager(None, root=tmp_path / "A")
        mm.create("m")
        chat = ChatManager(mm)
        for key, name, body in (("short", "a", "b"), ("bad key!!", "a", "b"),
                                (KEY_A, "  ", "b"), (KEY_A, "a", " \n "),
                                (KEY_A, "a", "x" * 4001)):
            with pytest.raises(ChatError):
                await chat.post("m", key, name, body)
        with pytest.raises(MeshError):
            await chat.post("nope", KEY_A, "a", "b")
        assert chat.messages("m")["last_seq"] == 0
        long = await chat.post("m", KEY_A, "n" * 99, "ok")
        assert len(long["message"]["name"]) == mesh_chat.MAX_NAME

    asyncio.run(run())


def test_c3_guest_post_is_sequenced_by_authority_and_pushes_back(home, tmp_path):
    async def run():
        mm_a, mm_b, chat_a, chat_b = _pair(tmp_path)
        out = await chat_b.post("m@pcA", KEY_B, "Bob", "from B")
        assert out["message"]["seq"] == 1 and out["message"]["machine"] == "pcB"
        assert chat_a.messages("m")["messages"][0]["body"] == "from B"
        assert chat_b.messages("m@pcA")["last_seq"] == 1
        await chat_a.post("m", KEY_A, "Alice", "from A")
        await _settle()
        rows = chat_b.room(mm_b.get("m@pcA")).messages
        assert [r["body"] for r in rows] == ["from B", "from A"]
        assert rows[1]["machine"] == "pcA"
        assert (tmp_path / "B" / "m@pcA" / "chat.jsonl").is_file()
        summary = chat_b.rooms()["rooms"][0]
        assert summary["local"] is False and summary["authority"] == "pcA"
        assert summary["address"] == "m@pcA"

    asyncio.run(run())


def test_c4_guest_catches_up_by_pulling(home, tmp_path):
    async def run():
        mm_a, mm_b, chat_a, chat_b = _pair(tmp_path)
        real = mm_a.peer_transport

        async def drop_pushes(machine, path, body):
            if path == "/peer/chat/sync":
                raise PeerUnreachable("relay down")
            return await real(machine, path, body)

        mm_a.peer_transport = drop_pushes
        for i in range(3):
            await chat_a.post("m", KEY_A, "Alice", f"m{i}")
        await _settle()
        room_b = chat_b.room(mm_b.get("m@pcA"))
        assert room_b.last_seq == 0
        # a sync that skips ahead is refused with our true position
        assert chat_b.peer_sync_accept(
            "m", "pcA", "a-to-b", [chat_a.room(mm_a.get("m")).messages[2]]
        ) == {"resync": 0}
        chat_b.messages("m@pcA")  # a page poll starts the pull
        await _settle()
        assert [r["body"] for r in room_b.messages] == ["m0", "m1", "m2"]
        # a guest post while behind still ends up complete and in order
        await chat_a.post("m", KEY_A, "Alice", "m3")
        await _settle()
        out = await chat_b.post("m@pcA", KEY_B, "Bob", "reply")
        assert out["message"]["seq"] == 5
        assert [r["seq"] for r in room_b.messages] == [1, 2, 3, 4, 5]

    asyncio.run(run())


def test_c5_unreachable_authority_refuses_the_post(home, tmp_path):
    async def run():
        mm_a, mm_b, chat_a, chat_b = _pair(tmp_path)

        async def down(machine, path, body):
            raise PeerUnreachable("relay down")

        mm_b.peer_transport = down
        with pytest.raises(ChatUnavailable):
            await chat_b.post("m@pcA", KEY_B, "Bob", "lost?")
        assert chat_b.room(mm_b.get("m@pcA")).last_seq == 0
        assert chat_a.room(mm_a.get("m")).last_seq == 0

    asyncio.run(run())


def test_c6_wrong_link_token_is_refused(home, tmp_path):
    mm_a, mm_b, chat_a, chat_b = _pair(tmp_path)
    msg = {"key": KEY_B, "name": "Mallory", "body": "x"}
    with pytest.raises(MeshError):
        chat_a.peer_send_accept("m", "pcB", "wrong", msg)
    with pytest.raises(MeshError):
        chat_a.peer_pull_accept("m", "pcB", "wrong", 0)
    with pytest.raises(MeshError):  # only the authority may push to a mirror
        chat_b.peer_sync_accept("m", "pcX", "a-to-b", [])
    with pytest.raises(MeshError):  # a mirror does not sequence
        chat_b.peer_send_accept("m", "pcA", "a-to-b", msg)
    assert chat_a.room(mm_a.get("m")).last_seq == 0


def test_c7_real_federated_join_carries_chat(home, tmp_path):
    from test_mesh_join import _manager, _primary_with_alice, _register_py_harness
    from test_mesh_join import _dispatch_peer as _mesh_dispatch

    _register_py_harness()

    async def run():
        mgr = _manager()
        mm_a, mm_b = await _primary_with_alice(mgr, tmp_path)
        machines = {"pcA": mm_a, "pcB": mm_b}
        chats = {"pcA": ChatManager(mm_a), "pcB": ChatManager(mm_b)}
        chat_call = _chat_dispatch(chats)

        async def call(machine, path, body):
            if path.startswith("/peer/chat/"):
                return await chat_call(machine, path, body)
            result = _mesh_dispatch(machines[machine], path, body)
            if asyncio.iscoroutine(result):
                result = await result
            return result

        for name, mm in machines.items():
            mm.machine = name
            mm.peer_transport = call
            mm.relay_connected = lambda: True
        code = mm_a.invite("m")["code"]
        await mm_b.join("m@pcA", "sb", handle="bob", code=code)
        await chats["pcB"].post("m@pcA", KEY_B, "Bob", "hello A")
        await chats["pcA"].post("m", KEY_A, "Alice", "hello B")
        await _settle()
        for chat, ref in ((chats["pcA"], "m"), (chats["pcB"], "m@pcA")):
            rows = chat.room(chat.mm.get(ref)).messages
            assert [(r["seq"], r["body"]) for r in rows] == [
                (1, "hello A"), (2, "hello B"),
            ]
        await mgr.shutdown_all()

    asyncio.run(run())


def test_c8_http_routes(home, tmp_path):
    from aiohttp import web
    from aiohttp.test_utils import TestClient, TestServer

    async def run():
        mm = MeshManager(None, root=tmp_path / "A")
        mm.create("m")
        app = web.Application()
        app["mesh"] = mm
        mesh_chat.install(app)
        async with TestClient(TestServer(app)) as client:
            r = await client.post("/api/chat/rooms/m/messages",
                                  json={"key": KEY_A, "name": "Alice", "body": "hi"})
            assert r.status == 200 and (await r.json())["message"]["seq"] == 1
            r = await client.post("/api/chat/rooms/m/messages",
                                  json={"key": KEY_A, "name": "Alice", "body": ""})
            assert r.status == 400
            r = await client.post("/api/chat/rooms/zz/messages",
                                  json={"key": KEY_A, "name": "Alice", "body": "x"})
            assert r.status == 404
            r = await client.get("/api/chat/rooms")
            body = await r.json()
            assert body["rooms"][0]["last_seq"] == 1
            r = await client.get("/api/chat/rooms/m/messages?after=0")
            assert [m["body"] for m in (await r.json())["messages"]] == ["hi"]
            r = await client.get("/api/chat/rooms/m/messages?after=x")
            assert r.status == 400
            r = await client.post("/peer/chat/pull",
                                  json={"mesh": "m", "machine": "pcB", "token": "t"})
            assert r.status == 400

    asyncio.run(run())
