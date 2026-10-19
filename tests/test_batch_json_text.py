"""A batched read carries the route's JSON as written (claunch-xrrn0).

``_batch_one`` used to parse each route's body and the batch (or the control
socket's ``read_result``) encoded it again, both on the loop that pumps the
terminals. Now a JSON body is handed through as :class:`api.JSONText` and
spliced into the reply. What must not change: the answer equals the direct
GET, a body that is not JSON goes the old way, and a large answer still goes
out in parts (claunch-iss86).
"""

from __future__ import annotations

import asyncio
import json
import time

from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import api as api_mod
from claude_launcher.daemon import channel
from claude_launcher.daemon.api import JSONText, _with_answers, build_app
from claude_launcher.daemon.manager import SessionManager

BEARER = {"Authorization": "Bearer sekrit"}


async def _plain_list(request):
    return web.Response(text="[1, 2]", content_type="text/plain")


async def _plain_prose(request):
    return web.Response(text="not json", content_type="text/plain")


_ticks = [0]


async def _ticking(request):
    """An answer that is different on every read."""
    _ticks[0] += 1
    return web.json_response({"tick": _ticks[0]})


async def _serve():
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    app = build_app(mgr, "sekrit", started_at=time.monotonic())
    app.router.add_get("/api/_test/plain-list", _plain_list)
    app.router.add_get("/api/_test/plain-prose", _plain_prose)
    app.router.add_get("/api/_test/ticking", _ticking)
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


async def _read(ws, frame):
    await ws.send_json(frame)
    while True:
        msg = await asyncio.wait_for(ws.receive(), 10)
        data = json.loads(msg.data)
        if data.get("type") == "read_result":
            return msg.data, data


def test_a_json_route_is_handed_through_as_text(home):
    """The route's own text, not a parse of it, and the same text a direct
    GET returns."""

    async def run():
        client = await _serve()
        try:
            template = await client.get("/api/health", headers=BEARER)
            direct = await template.text()
            seen = []

            real = api_mod._batch_one

            async def spy(tmpl, path):
                value = await real(tmpl, path)
                seen.append(value)
                return value

            api_mod._batch_one = spy
            try:
                async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                    await ws.receive()  # init
                    _, data = await _read(ws, {"type": "read", "id": 1,
                                               "paths": ["/api/health"]})
            finally:
                api_mod._batch_one = real
            assert len(seen) == 1 and isinstance(seen[0], JSONText)
            assert data["errors"] == {}
            assert data["answers"]["/api/health"].keys() == json.loads(direct).keys()
        finally:
            await client.close()

    asyncio.run(run())


def test_batch_and_control_read_equal_the_direct_get(home):
    async def run():
        client = await _serve()
        try:
            paths = ["/api/sessions", "/api/workspaces"]
            direct = {p: await (await client.get(p, headers=BEARER)).json() for p in paths}
            body = await (await client.post("/api/batch", json={"paths": paths},
                                            headers=BEARER)).json()
            assert body["answers"] == direct
            assert body["errors"] == {} and body["statuses"] == {}
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive()  # init
                _, data = await _read(ws, {"type": "read", "id": 9, "paths": paths})
            assert data["id"] == 9
            assert data["answers"] == direct
        finally:
            await client.close()

    asyncio.run(run())


def test_a_body_that_is_not_json_goes_the_old_way(home):
    """Not ``application/json``: parsed as before -- a value if it parses,
    an error under its path if not, and the other answers unharmed."""

    async def run():
        client = await _serve()
        try:
            paths = ["/api/_test/plain-list", "/api/_test/plain-prose", "/api/health"]
            body = await (await client.post("/api/batch", json={"paths": paths},
                                            headers=BEARER)).json()
            assert body["answers"]["/api/_test/plain-list"] == [1, 2]
            assert "/api/_test/plain-prose" in body["errors"]
            assert "/api/health" in body["answers"]
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive()  # init
                _, data = await _read(ws, {"type": "read", "id": 2, "paths": paths})
            assert data["answers"]["/api/_test/plain-list"] == [1, 2]
            assert "/api/_test/plain-prose" in data["errors"]
        finally:
            await client.close()

    asyncio.run(run())


def test_the_spliced_reply_is_the_json_of_the_whole():
    answers = {"/a": JSONText('{"t": "한글", "n": [1, 2]}'), "/b": {"x": None},
               "/c": None}
    head = {"type": "read_result", "id": 3, "errors": {"/d": "boom"}, "statuses": {}}
    assert json.loads(_with_answers(head, answers)) == {
        **head, "answers": {"/a": {"t": "한글", "n": [1, 2]}, "/b": {"x": None},
                            "/c": None}}
    assert json.loads(_with_answers({}, {})) == {"answers": {}}


class _Sink:
    closed = False

    def __init__(self):
        self.sent = []

    async def send_str(self, data):
        self.sent.append(data)

    async def send_bytes(self, data):
        self.sent.append(data)

    async def close(self, *, code=1000, message=b""):
        self.closed = True


def test_pre_encoded_text_still_goes_in_parts_held_to_the_window():
    """The bulk lane's parts (claunch-iss86) apply to a reply that arrives
    as text exactly as to one that arrives as a dict."""

    async def run():
        ws = _Sink()
        carrier = channel.Carrier(ws, None, None)
        carrier.PART_CHARS = 10
        carrier.BULK_WINDOW = 30
        text = _with_answers({"type": "read_result", "id": 7, "errors": {},
                              "statuses": {}},
                             {"/a": JSONText(json.dumps({"v": "x" * 60}))})
        sending = asyncio.ensure_future(carrier.send_bulk(text, parts=True, key=7))
        await asyncio.sleep(0.05)
        parts = [json.loads(f) for f in ws.sent]
        assert [p["seq"] for p in parts] == [0, 1, 2], "three parts fill the window"
        assert not sending.done()
        seq = 0
        while not sending.done():
            carrier.ack({"type": "read_ack", "id": 7, "seq": seq})
            seq += 1
            await asyncio.sleep(0.01)
        parts = [json.loads(f) for f in ws.sent]
        assert not parts[-1]["more"] and all(p["more"] for p in parts[:-1])
        assert "".join(p["data"] for p in parts) == text

        # Small, or a page without parts: the text is the frame, unchanged.
        ws.sent.clear()
        await asyncio.wait_for(carrier.send_bulk(text), 2)
        await asyncio.wait_for(carrier.send_bulk('{"id": 2}', parts=True, key=2), 2)
        assert ws.sent == [text, '{"id": 2}']
        await carrier.shutdown()

    asyncio.run(run())


def test_a_read_that_says_what_it_holds_gets_only_what_changed(home):
    """The conditional read (claunch-4thlk.2) and its spans (claunch-4thlk.4).

    A read that carries ``hashes`` gets every answer's digest back; sent
    again with those digests, an answer that has not changed is left out and
    named in ``unchanged``. ``spans`` says where each answer's text sits in
    the reply, counted the way the page's ``String.slice`` counts. A read
    without either is answered exactly as before."""

    async def run():
        client = await _serve()
        try:
            async with client.ws_connect("/api/control/ws", headers=BEARER) as ws:
                await ws.receive()  # init
                paths = ["/api/_test/plain-list", "/api/_test/ticking"]
                raw, first = await _read(ws, {"type": "read", "id": 1, "paths": paths,
                                              "hashes": {}, "spans": True})
                assert set(first["answers"]) == set(paths)
                assert first["unchanged"] == []
                assert set(first["hashes"]) == set(paths)
                units = raw.encode("utf-16-le")
                for path in paths:
                    start, end = first["spans"][path]
                    text = units[2 * start:2 * end].decode("utf-16-le")
                    assert json.loads(text) == first["answers"][path]
                # The ticking route changes on every read, so only the plain
                # list is the same twice; that is the one left out.
                _, second = await _read(ws, {"type": "read", "id": 2, "paths": paths,
                                             "hashes": first["hashes"], "spans": True})
                assert second["unchanged"] == ["/api/_test/plain-list"]
                assert list(second["answers"]) == ["/api/_test/ticking"]
                # A digest the page does not hold is just a full answer.
                _, third = await _read(ws, {"type": "read", "id": 3, "paths": paths[:1],
                                            "hashes": {paths[0]: "0" * 16}})
                assert third["answers"] == {paths[0]: [1, 2]}
                assert "spans" not in third
                # An older page: no hashes, no spans, the reply it always had.
                _, old = await _read(ws, {"type": "read", "id": 4, "paths": paths[:1]})
                assert old["answers"] == {paths[0]: [1, 2]}
                assert "hashes" not in old and "unchanged" not in old
        finally:
            await client.close()

    asyncio.run(run())


def test_spans_count_utf16_units_past_korean_and_astral_text():
    text = _with_answers(
        {"type": "read_result", "id": 9},
        {"/a": JSONText('{"t": "한글 😀"}'), "/b": [1, "😀"]},
        spans=True,
    )
    data = json.loads(text)
    units = text.encode("utf-16-le")
    for path, (start, end) in data["spans"].items():
        assert json.loads(units[2 * start:2 * end].decode("utf-16-le")) == data["answers"][path]
