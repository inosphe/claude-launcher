"""The web debug page's daemon side (daemon/webdebug.py, claunch-dsb28): the
per-origin proxy that injects the probe, the window registry the session's
commands go through, the user's explicit send, and the MCP tools."""

import asyncio
import json

import aiohttp
import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import mcp_server, webdebug_mcp
from claude_launcher.daemon import webdebug


# --------------------------------------------------------------------------- #
# pure helpers
# --------------------------------------------------------------------------- #
@pytest.mark.parametrize("raw, origin, path", [
    ("localhost:5173", "http://localhost:5173", "/"),
    ("http://LocalHost:3000/a/b?x=1#top", "http://localhost:3000", "/a/b?x=1#top"),
    ("https://example.test", "https://example.test", "/"),
    ("http://[::1]:8080/x", "http://[::1]:8080", "/x"),
])
def test_split_target(raw, origin, path):
    assert webdebug.split_target(raw) == (origin, path)


@pytest.mark.parametrize("raw", ["", "file:///etc/passwd", "ftp://x/", "http://", "http://h:99999/"])
def test_split_target_refuses(raw):
    with pytest.raises(webdebug.WebDebugError):
        webdebug.split_target(raw)


def test_probe_goes_first_in_head_and_never_before_the_doctype():
    tag = webdebug.PROBE_TAG
    assert webdebug.inject_probe(b"<!doctype html><html><head><title>t</title>") == (
        b"<!doctype html><html><head>" + tag + b"<title>t</title>")
    assert webdebug.inject_probe(b'<HTML lang="ko"><body>x') == b'<HTML lang="ko">' + tag + b"<body>x"
    assert webdebug.inject_probe(b"<!DOCTYPE html><p>x") == b"<!DOCTYPE html>" + tag + b"<p>x"
    assert webdebug.inject_probe(b"<p>x") == tag + b"<p>x"
    # <header> is not <head>
    assert webdebug.inject_probe(b"<html><header>").startswith(b"<html>" + tag)


def test_set_cookie_is_made_to_stick_on_the_proxy():
    out = webdebug._clean_set_cookie("sid=1; Domain=example.test; Path=/; Secure; SameSite=None; HttpOnly")
    assert out == "sid=1; Path=/; HttpOnly"


def test_the_send_block_names_the_window_and_the_element(tmp_path):
    img = tmp_path / "a.png"
    text = webdebug.compose_send("w-abcd", {
        "kind": "element", "url": "http://localhost:5173/x", "title": "X", "note": "이 버튼이\n안 눌린다",
        "element": {"label": "button#save", "selector": "#save", "text": "Save",
                    "rect": {"x": 1, "y": 2, "width": 3, "height": 4}, "html": "<button id=save>Save</button>"},
    }, img)
    lines = text.splitlines()
    assert lines[0] == "[claunch webdebug w-abcd] The user sent you an element they picked from the web debug page."
    assert "  > 이 버튼이" in lines and "  > 안 눌린다" in lines
    assert "  selector: #save" in lines
    assert f"  image: {img} (open it to see what the user means)" in lines
    assert 'window="w-abcd"' in lines[-1]


# --------------------------------------------------------------------------- #
# the proxy, end to end against a real upstream
# --------------------------------------------------------------------------- #
def _upstream_app(seen):
    async def page(request):
        seen.append(dict(request.headers))
        resp = web.Response(text="<!doctype html><html><head><title>dev</title></head><body>hi</body></html>",
                            content_type="text/html")
        resp.headers["Content-Security-Policy"] = "default-src 'self'"
        resp.headers["X-Frame-Options"] = "DENY"
        resp.headers["Set-Cookie"] = "sid=1; Domain=localhost; Secure; Path=/"
        return resp

    async def asset(request):
        return web.Response(text="console.log(1)", content_type="application/javascript")

    async def moved(request):
        raise web.HTTPFound(f"http://{request.host}/asset.js")

    async def sock(request):
        ws = web.WebSocketResponse()
        await ws.prepare(request)
        async for msg in ws:
            if msg.type == WSMsgType.TEXT:
                await ws.send_str("echo:" + msg.data)
        return ws

    app = web.Application()
    app.router.add_get("/", page)
    app.router.add_get("/asset.js", asset)
    app.router.add_get("/moved", moved)
    app.router.add_get("/ws", sock)
    return app


def test_the_proxy_injects_the_probe_and_guards_its_port():
    async def run():
        seen = []
        up = TestServer(_upstream_app(seen))
        await up.start_server()
        proxy = webdebug.Proxy(f"http://127.0.0.1:{up.port}", "127.0.0.1")
        await proxy.start()
        try:
            client = TestClient(TestServer(web.Application()))  # only for its session
            await client.start_server()
            http = client.session
            base = f"http://127.0.0.1:{proxy.port}"

            r = await http.get(base + "/", allow_redirects=False)
            assert r.status == 403  # no cookie: not served
            r = await http.get(base + webdebug.ENTER_PATH + "?k=wrong&to=/", allow_redirects=False)
            assert r.status == 403

            enter = proxy.enter_path("/") + "&parent=" + "http%3A%2F%2Fdash.test%3A8378"
            r = await http.get(base + enter, allow_redirects=False)
            assert r.status == 302 and r.headers["Location"] == "/"
            assert proxy.cookie in r.cookies
            assert proxy.parents == {"http://dash.test:8378"}

            # The daemon's login cookie rides along to every port of the host;
            # it must not reach the page under test.
            r = await http.get(base + "/", headers={"Cookie": f"{proxy.cookie}={proxy.secret}; "
                                                              "claunch_session_abc=SECRET; app=1"})
            body = await r.text()
            assert r.status == 200
            assert "<head>" + webdebug.PROBE_TAG.decode() + "<title>" in body
            assert "Content-Security-Policy" not in r.headers and "X-Frame-Options" not in r.headers
            assert "Domain" not in r.headers["Set-Cookie"] and "Secure" not in r.headers["Set-Cookie"]
            sent = seen[-1]
            assert sent.get("Cookie") == "app=1"
            assert sent["Host"] == f"127.0.0.1:{up.port}"

            cookie = {"Cookie": f"{proxy.cookie}={proxy.secret}"}
            r = await http.get(base + "/asset.js", headers=cookie)
            assert await r.text() == "console.log(1)"  # not HTML: untouched
            r = await http.get(base + "/moved", headers=cookie, allow_redirects=False)
            assert r.headers["Location"] == "/asset.js"  # kept on the proxy

            r = await http.get(base + webdebug.PROBE_PATH, headers=cookie)
            probe = await r.text()
            assert probe.startswith("(function(CONFIG){")
            assert '"parents": ["http://dash.test:8378"]' in probe

            ws = await http.ws_connect(base.replace("http", "ws") + "/ws", headers=cookie)
            await ws.send_str("x")
            msg = await ws.receive(timeout=5)
            assert msg.data == "echo:x"
            await ws.close()
            await client.close()
        finally:
            await proxy.stop()
            await up.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the scheme the address was typed with (claunch-o8pb7)
# --------------------------------------------------------------------------- #
def test_the_scheme_probe_reads_a_plaintext_peer():
    async def run():
        up = TestServer(_upstream_app([]))
        await up.start_server()
        try:
            assert await webdebug._speaks_tls("127.0.0.1", up.port) is False
        finally:
            await up.close()

    asyncio.run(run())


def test_an_unreachable_address_keeps_the_scheme_it_was_typed_with():
    async def run():
        # Nothing listens: no answer is not a verdict about TLS.
        assert await webdebug.alternate_scheme("https://127.0.0.1:1/") is None

    asyncio.run(run())


def test_an_https_address_for_a_plaintext_server_is_opened_as_http():
    async def run():
        up = TestServer(_upstream_app([]))
        await up.start_server()
        client = TestClient(TestServer(_app(_Manager())))
        await client.start_server()
        try:
            r = await client.post("/api/webdebug/proxy", json={"url": f"https://127.0.0.1:{up.port}/x"})
            body = await r.json()
            assert r.status == 200
            assert body["origin"] == f"http://127.0.0.1:{up.port}"
            assert body["switched_from"] == f"https://127.0.0.1:{up.port}"
            assert body["path"] == "/x"
            # The frame's base is what the browser that asked can reach.
            assert body["proxy_origin"] == f"http://127.0.0.1:{body['port']}"
        finally:
            await client.close()
            await up.close()

    asyncio.run(run())


def test_a_plain_http_address_is_not_probed_or_rewritten():
    async def run():
        client = TestClient(TestServer(_app(_Manager())))
        await client.start_server()
        try:
            body = await (await client.post("/api/webdebug/proxy",
                                            json={"url": "http://127.0.0.1:5173/"})).json()
            assert body["origin"] == "http://127.0.0.1:5173"
            assert body["switched_from"] is None
        finally:
            await client.close()

    asyncio.run(run())


def test_the_scheme_mismatch_page_names_the_fix():
    text = webdebug._upstream_failure(
        "https://dev.test:5173/",
        OSError("Cannot connect to host dev.test:5173 ssl:False "
                "[[SSL: WRONG_VERSION_NUMBER] wrong version number (_ssl.c:1082)]"))
    assert "http://dev.test:5173/" in text and "TLS" in text
    text = webdebug._upstream_failure("http://dev.test:8443/", aiohttp.ServerDisconnectedError())
    assert "https://dev.test:8443/" in text


def test_the_page_frames_the_proxy_origin_the_daemon_returned():
    """The frame must not be built from location.protocol: the proxy speaks
    plain http, so an https dashboard built an https frame address that no
    listener answers (claunch-o8pb7)."""
    page = (webdebug.PAGE_FILE.parent / "webdebug.js").read_text(encoding="utf-8")
    assert "res.proxy_origin" in page
    assert "${location.protocol}" not in page


# --------------------------------------------------------------------------- #
# windows and commands through the routes
# --------------------------------------------------------------------------- #
class _Session:
    def __init__(self, name):
        self.sdef = type("Def", (), {"name": name, "cwd": "/w", "harness": ""})()
        self.delivered = []

    def status(self):
        return "idle"

    def queue_delivery(self, text, **kw):
        self.delivered.append(text)
        return True


class _Manager:
    def __init__(self, *names):
        self.sessions = {n: _Session(n) for n in names}

    def get(self, name):
        if name not in self.sessions:
            raise KeyError(name)
        return self.sessions[name]

    def list(self):
        return list(self.sessions.values())

    def wake_soon(self, *a, **kw):
        return False


def _app(manager):
    app = web.Application()
    app["manager"] = manager
    app["websockets"] = set()
    webdebug.install(app)
    return app


def test_a_command_reaches_the_window_showing_that_session_and_only_it(tmp_path, monkeypatch):
    monkeypatch.setattr(webdebug, "image_folder", lambda: tmp_path)

    async def run():
        client = TestClient(TestServer(_app(_Manager("s1", "s2"))))
        await client.start_server()
        try:
            r = await client.post("/api/webdebug/command", json={"session": "s1", "op": "snapshot"})
            assert r.status == 404 and "web" in (await r.json())["error"]

            ws = await client.ws_connect("/api/webdebug/ws?window=w-abcd")
            await ws.send_json({"type": "hello", "session": "s1", "url": "http://localhost:5173/"})
            await asyncio.sleep(0.05)

            listed = await (await client.get("/api/webdebug/windows?session=s1")).json()
            assert [w["window"] for w in listed["windows"]] == ["w-abcd"]

            # another session may not drive it
            r = await client.post("/api/webdebug/command",
                                  json={"session": "s2", "op": "click", "window": "w-abcd"})
            assert r.status == 403
            r = await client.post("/api/webdebug/command", json={"session": "s1", "op": "rm -rf"})
            assert r.status == 400

            async def page():
                msg = await ws.receive_json(timeout=5)
                assert msg["type"] == "command" and msg["op"] == "click" and msg["args"] == {"ref": "e3"}
                await ws.send_json({"type": "result", "id": msg["id"], "ok": True, "value": {"clicked": "e3"}})
                msg = await ws.receive_json(timeout=5)
                (tmp_path / "0123456789abcdef0123.png").write_bytes(b"png")
                await ws.send_json({"type": "result", "id": msg["id"], "ok": True,
                                    "value": {"image": "0123456789abcdef0123.png"}})
                msg = await ws.receive_json(timeout=5)
                await ws.send_json({"type": "result", "id": msg["id"], "ok": False, "error": "no screen share"})

            task = asyncio.ensure_future(page())
            r = await client.post("/api/webdebug/command",
                                  json={"session": "s1", "op": "click", "args": {"ref": "e3"}})
            assert r.status == 200
            assert (await r.json())["value"] == {"clicked": "e3"}
            r = await client.post("/api/webdebug/command", json={"session": "s1", "op": "screenshot"})
            assert (await r.json())["value"]["image"] == str(tmp_path / "0123456789abcdef0123.png")
            r = await client.post("/api/webdebug/command", json={"session": "s1", "op": "screenshot"})
            assert r.status == 422 and (await r.json())["error"] == "no screen share"
            await task

            # the page that never answers
            r = await client.post("/api/webdebug/command",
                                  json={"session": "s1", "op": "snapshot", "timeout": 1})
            assert r.status == 504
            await ws.close()
            await asyncio.sleep(0.05)
            assert (await (await client.get("/api/webdebug/windows")).json())["windows"] == []
        finally:
            await client.close()

    asyncio.run(run())


def test_send_types_the_block_into_the_session(tmp_path, monkeypatch):
    monkeypatch.setattr(webdebug, "image_folder", lambda: tmp_path)
    manager = _Manager("s1")

    async def run():
        client = TestClient(TestServer(_app(manager)))
        await client.start_server()
        try:
            r = await client.post("/api/webdebug/images", data=b"\x89PNG....",
                                  headers={"Content-Type": "image/png"})
            image = (await r.json())["id"]
            assert (tmp_path / image).read_bytes() == b"\x89PNG...."
            r = await client.post("/api/webdebug/images", data=b"x", headers={"Content-Type": "text/plain"})
            assert r.status == 415

            r = await client.post("/api/webdebug/send", json={"session": "s1", "window": "w-abcd",
                                                              "kind": "screenshot"})
            assert r.status == 400  # nothing to send
            r = await client.post("/api/webdebug/send", json={"session": "nobody", "window": "w-abcd",
                                                              "kind": "note", "note": "hi"})
            assert r.status == 404
            r = await client.post("/api/webdebug/send", json={
                "session": "s1", "window": "w-abcd", "kind": "screenshot", "image": image,
                "url": "http://localhost:5173/", "note": "여기 깨짐"})
            assert r.status == 200 and (await r.json())["delivered"] is True
            [text] = manager.sessions["s1"].delivered
            assert text.startswith("[claunch webdebug w-abcd] The user sent you a screenshot")
            assert str(tmp_path / image) in text and "  > 여기 깨짐" in text

            r = await client.post("/api/webdebug/send", json={"session": "s1", "window": "w-abcd",
                                                              "image": "../../etc/passwd"})
            assert r.status == 400

            sessions = await (await client.get("/api/webdebug/sessions")).json()
            assert sessions == {"sessions": [{"name": "s1", "status": "idle", "cwd": "/w"}]}
            r = await client.get("/webdebug")
            assert r.status == 200 and "wd-frame" in await r.text()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_proxy_route_opens_one_listener_per_origin():
    async def run():
        client = TestClient(TestServer(_app(_Manager())))
        await client.start_server()
        try:
            a = await (await client.post("/api/webdebug/proxy", json={"url": "localhost:5173/x"})).json()
            b = await (await client.post("/api/webdebug/proxy", json={"url": "http://localhost:5173/y"})).json()
            c = await (await client.post("/api/webdebug/proxy", json={"url": "localhost:5174"})).json()
            assert a["origin"] == "http://localhost:5173" and a["path"] == "/x"
            assert a["port"] == b["port"] != c["port"]
            assert a["enter"].startswith(webdebug.ENTER_PATH + "?k=")
            r = await client.post("/api/webdebug/proxy", json={"url": "javascript:alert(1)"})
            assert r.status == 400
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the daemon refuses the login cookie from a proxied page
# --------------------------------------------------------------------------- #
def test_the_login_cookie_does_not_work_from_another_port_of_the_host(home):
    """A proxied page is on the dashboard's host at another port: the same
    site, so the browser attaches the SameSite=Strict login cookie to what it
    sends the daemon. The daemon must refuse it there (claunch-dsb28 review)."""
    import time

    from claude_launcher.daemon.api import build_app, cookie_name
    from claude_launcher.daemon.manager import SessionManager

    token = "token-x"

    async def run():
        mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
        client = TestClient(TestServer(build_app(mgr, token, started_at=time.monotonic())))
        await client.start_server()
        try:
            login = await client.post("/api/auth/session", json={"token": token})
            assert login.status == 200
            jar = {cookie_name(token): login.cookies[cookie_name(token)].value}
            client.session.cookie_jar.clear()
            own = f"http://{client.host}:{client.port}"
            proxy = f"http://{client.host}:{client.port + 1}"

            async def status(method, path, headers, cookies=jar, **kw):
                async with client.request(method, path, headers=headers, cookies=cookies, **kw) as r:
                    return r.status

            # A write from the proxied page: no-cors text/plain POST, cookie attached.
            body = {"data": '{"url": "localhost:1"}'}
            assert await status("POST", "/api/webdebug/proxy", {"Origin": proxy}, **body) == 403
            assert await status("POST", "/api/webdebug/proxy",
                                {"Sec-Fetch-Site": "same-site", "Origin": proxy}, **body) == 403
            assert await status("GET", "/api/sessions", {"Sec-Fetch-Site": "cross-site"}) == 403
            # A WebSocket is outside CORS altogether; the upgrade is refused before it opens.
            ws_headers = {"Origin": proxy, "Sec-Fetch-Site": "same-site", "Connection": "Upgrade",
                          "Upgrade": "websocket", "Sec-WebSocket-Version": "13",
                          "Sec-WebSocket-Key": "dGhlIHNhbXBsZSBub25jZQ=="}
            assert await status("GET", "/api/webdebug/ws?window=w-1", ws_headers) == 403
            # What can change something under /vscode/ is refused too.
            assert await status("POST", "/vscode/x", {"Sec-Fetch-Site": "same-site"}) == 403

            # The dashboard's own requests, a typed address and shells still pass.
            assert await status("GET", "/api/sessions", {"Sec-Fetch-Site": "same-origin"}) == 200
            assert await status("GET", "/api/sessions", {"Sec-Fetch-Site": "none"}) == 200
            assert await status("GET", "/api/sessions", {"Origin": own}) == 200
            assert await status("GET", "/api/sessions", {}) == 200
            bearer = {"Authorization": f"Bearer {token}", "Origin": proxy, "Sec-Fetch-Site": "same-site"}
            assert await status("GET", "/api/sessions", bearer, cookies=None) == 200
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the MCP tools
# --------------------------------------------------------------------------- #
class _Client:
    def __init__(self):
        self.calls = []

    def get(self, path, **kw):
        self.calls.append(("GET", path, None))
        return {}

    def post(self, path, body=None, **kw):
        self.calls.append(("POST", path, body, kw))
        return {}


def test_the_tools_carry_the_session_and_the_op(monkeypatch):
    client = _Client()
    monkeypatch.setattr(webdebug_mcp, "_client", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    webdebug_mcp.call_tool("webdebug_windows", {})
    assert client.calls[-1][:2] == ("GET", "/api/webdebug/windows?session=s7")
    webdebug_mcp.call_tool("webdebug_click", {"ref": "e2", "window": "w-abcd"})
    _, path, body, kw = client.calls[-1]
    assert path == "/api/webdebug/command"
    assert body == {"session": "s7", "op": "click", "args": {"ref": "e2"}, "timeout": 20, "window": "w-abcd"}
    assert kw["timeout"] > body["timeout"]
    webdebug_mcp.call_tool("webdebug_navigate", {"url": "localhost:3000"})
    assert client.calls[-1][2]["timeout"] == 30
    for tool in webdebug_mcp.TOOLS:
        if tool["name"] != "webdebug_windows":
            assert webdebug_mcp._OPS[tool["name"]] in webdebug.OPS
    names = {t["name"] for t in mcp_server.TOOLS}
    assert {t["name"] for t in webdebug_mcp.TOOLS} <= names
