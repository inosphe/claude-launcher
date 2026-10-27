"""The web debug page: a browser the daemon runs, and the screen of it the
user watches (claunch-o8pb7.2).

The proxy, the injected probe and the iframe are gone; these tests hold the
shape that replaced them:

* no second listener and no framing -- the module and the page assets carry
  no proxy, probe or iframe;
* the browser is found the way the design says ($CLAUNCH_CHROMIUM, then the
  Playwright cache, then the system), and an address is normalised before it
  is opened;
* a window belongs to the session the user picked and to no other;
* the agent script is installed into every document before the page's own
  scripts, and commands run through it;
* one browser-level test drives the real Chromium -- frames, input and a
  screenshot -- and skips when this machine has no browser.
"""
from __future__ import annotations

import asyncio
import json
import pathlib
import stat
from pathlib import Path

import pytest
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher.daemon import webdebug

STATIC = Path(webdebug.__file__).resolve().parent.parent / "web" / "static"


# --------------------------------------------------------------------------- #
# the shape that replaced the proxy
# --------------------------------------------------------------------------- #
def test_the_page_is_a_screen_and_not_a_frame():
    html = (STATIC / "webdebug.html").read_text(encoding="utf-8")
    script = (STATIC / "webdebug.js").read_text(encoding="utf-8")
    assert "<iframe" not in html and "iframe" not in script
    assert "wd-screen" in html and "wd-frame" not in html
    assert "postMessage" not in script
    assert "webdebug-probe.js" not in html and not (STATIC / "webdebug-probe.js").exists()


def test_the_daemon_carries_no_proxy_and_no_probe_injection():
    source = pathlib.Path(webdebug.__file__).read_text(encoding="utf-8")
    for gone in ("class Proxy", "def _location(", "def alternate_scheme(",
                 "def _speaks_tls(", "def inject_probe(", "_DROP_RESPONSE",
                 "/api/webdebug/proxy"):
        assert gone not in source, f"{gone} should be gone with the framed design"


def test_the_agent_is_installed_into_every_document_before_the_page_runs():
    source = pathlib.Path(webdebug.__file__).read_text(encoding="utf-8")
    assert "Page.addScriptToEvaluateOnNewDocument" in source
    agent = (STATIC / "webdebug-agent.js").read_text(encoding="utf-8")
    assert "__claunchWebdebug" in agent and "postMessage" not in agent


# --------------------------------------------------------------------------- #
# the browser
# --------------------------------------------------------------------------- #
def test_the_browser_is_taken_from_the_environment_first(tmp_path, monkeypatch):
    binary = tmp_path / "chrome"
    binary.write_text("#!/bin/sh\nexit 0\n")
    binary.chmod(binary.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv(webdebug.BROWSER_ENV, str(binary))
    assert webdebug.find_browser() == str(binary)


def test_a_browser_that_is_not_there_is_named_not_guessed(monkeypatch):
    monkeypatch.setenv(webdebug.BROWSER_ENV, "/nonexistent/chrome")
    monkeypatch.setattr(webdebug, "browser_candidates", lambda: ["/nonexistent/chrome"])
    with pytest.raises(webdebug.WebDebugError) as caught:
        webdebug.find_browser()
    assert caught.value.status == 503
    assert webdebug.BROWSER_ENV in str(caught.value)


@pytest.mark.parametrize("raw,opened", [
    ("localhost:5173/", "http://localhost:5173/"),
    ("127.0.0.1:8000", "http://127.0.0.1:8000"),
    ("https://example.test/x", "https://example.test/x"),
    ("about:blank", "about:blank"),
])
def test_an_address_gets_a_scheme_before_it_is_opened(raw, opened):
    assert webdebug.target_url(raw) == opened


@pytest.mark.parametrize("raw", ["", "   ", "javascript:alert(1)", "ftp://host/file"])
def test_only_addresses_a_browser_can_open_are_accepted(raw):
    with pytest.raises(webdebug.WebDebugError) as caught:
        webdebug.target_url(raw)
    assert caught.value.status == 400


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


class _FakeCDP:
    """Answers like the browser would, and records what was asked."""

    def __init__(self):
        self.sent = []
        self.closed = False

    async def send(self, method, params=None, *, session="", timeout=20):
        self.sent.append((method, params or {}, session))
        if method == "Target.createBrowserContext":
            return {"browserContextId": "ctx-1"}
        if method == "Target.createTarget":
            return {"targetId": "tgt-1"}
        if method == "Target.attachToTarget":
            return {"sessionId": "sess-1"}
        if method == "Page.navigate":
            return {"url": (params or {}).get("url", "")}
        if method == "Runtime.evaluate":
            expression = (params or {}).get("expression", "")
            if "document.title" in expression and "location.href" in expression:
                return {"result": {"value": json.dumps({"url": "http://localhost:5173/",
                                                        "title": "under test"})}}
            if "__claunchWebdebug" in expression:
                return {"result": {"value": {"elements": 'e1 button "go" @10,10 40x20',
                                             "url": "http://localhost:5173/", "title": "under test"}}}
            return {"result": {"value": None}}
        if method == "Page.captureScreenshot":
            return {"data": "aGVsbG8="}  # b"hello"
        return {}

    async def close(self):
        self.closed = True


class _FakeBrowser:
    def __init__(self):
        self.cdp = _FakeCDP()

    @property
    def live(self):
        return True

    async def ensure(self):
        return self.cdp

    async def new_page(self, url="about:blank"):
        return {"context_id": "ctx-1", "target_id": "tgt-1", "session_id": "sess-1"}

    async def close_page(self, page):
        return None

    async def stop(self):
        return None


class _Viewer:
    transport = None

    def __init__(self):
        self.json = []
        self.binary = []
        self.closed = False

    async def send_json(self, data):
        self.json.append(data)

    async def send_bytes(self, data):
        self.binary.append(data)

    async def close(self, **kw):
        self.closed = True


def _app(manager):
    app = webdebug.web.Application()
    app["manager"] = manager
    app["websockets"] = set()
    webdebug.install(app)
    return app


def test_a_window_belongs_to_the_session_the_user_picked(tmp_path, monkeypatch):
    monkeypatch.setattr(webdebug, "image_folder", lambda: tmp_path)

    async def run():
        hub = webdebug.Hub()
        hub.browser = _FakeBrowser()
        app = webdebug.web.Application()
        app["manager"] = _Manager("s1", "s2")
        app["websockets"] = set()
        webdebug.install(app, hub)
        client = TestClient(TestServer(app))
        await client.start_server()
        try:
            r = await client.post("/api/webdebug/command", json={"session": "s1", "op": "snapshot"})
            assert r.status == 404 and "web" in (await r.json())["error"]

            win = await hub.attach("w-abcd", "s1", _Viewer())
            await hub.navigate(win, "localhost:5173/")
            listed = await (await client.get("/api/webdebug/windows?session=s1")).json()
            assert [w["window"] for w in listed["windows"]] == ["w-abcd"]
            assert listed["windows"][0]["url"].startswith("http://localhost:5173")

            # another session may not drive it, and an unknown op never reaches
            # the browser at all
            r = await client.post("/api/webdebug/command",
                                  json={"session": "s2", "op": "click", "window": "w-abcd"})
            assert r.status == 403
            r = await client.post("/api/webdebug/command", json={"session": "s1", "op": "rm -rf"})
            assert r.status == 400

            r = await client.post("/api/webdebug/command",
                                  json={"session": "s1", "op": "snapshot", "window": "w-abcd"})
            body = await r.json()
            assert r.status == 200 and "e1 button" in body["value"]["elements"]

            r = await client.post("/api/webdebug/command",
                                  json={"session": "s1", "op": "navigate",
                                        "args": {"url": "https://example.test/"}, "window": "w-abcd"})
            body = await r.json()
            assert r.status == 200 and body["value"]["url"] == "http://localhost:5173/"
        finally:
            await hub.stop_all()
            await client.close()

    asyncio.run(run())


def test_the_screenshot_is_the_browsers_own_capture(tmp_path, monkeypatch):
    monkeypatch.setattr(webdebug, "image_folder", lambda: tmp_path)

    async def run():
        hub = webdebug.Hub()
        hub.browser = _FakeBrowser()
        try:
            win = await hub.attach("w-abcd", "s1", _Viewer())
            result = await hub.command("s1", "screenshot", {}, window="w-abcd")
            image = Path(result["value"]["image"])
            assert image.is_file() and image.read_bytes() == b"hello"
            assert result["value"]["bytes"] == 5
        finally:
            await hub.stop_all()

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
            assert r.status == 200 and "wd-screen" in await r.text()
        finally:
            await client.close()

    asyncio.run(run())


def test_the_send_block_names_the_window_and_the_element(tmp_path):
    image = tmp_path / "0123456789abcdef0123.png"
    image.write_bytes(b"png")
    text = webdebug.compose_send("w-abcd", {
        "kind": "element", "url": "http://localhost:5173/", "title": "under test", "note": "고쳐 주세요",
        "element": {"label": "button#save", "selector": "#save", "text": "저장",
                    "rect": {"x": 10, "y": 20, "width": 80, "height": 30},
                    "html": "<button id=save>저장</button>"},
    }, image)
    assert text.startswith("[claunch webdebug w-abcd] The user sent you an element they picked")
    assert "  page: http://localhost:5173/  (under test)" in text
    assert "  > 고쳐 주세요" in text
    assert "  selector: #save" in text and "  rect: x=10 y=20 width=80 height=30" in text
    assert str(image) in text
    assert "webdebug_screenshot" in text


# --------------------------------------------------------------------------- #
# the browser itself
# --------------------------------------------------------------------------- #
def test_a_real_browser_streams_frames_takes_input_and_screenshots(tmp_path, monkeypatch):
    try:
        webdebug.find_browser()
    except webdebug.WebDebugError:
        pytest.skip("no Chromium on this machine for the web debug page")
    monkeypatch.setattr(webdebug, "image_folder", lambda: tmp_path)
    page = tmp_path / "under_test.html"
    page.write_text(
        "<!doctype html><meta charset=utf-8><title>under test</title>"
        "<style>body{margin:0;background:#fff}#b{margin:40px;width:200px;height:60px}</style>"
        "<button id=b>go</button><script>window.__hits=0;"
        "document.getElementById('b').addEventListener('click',()=>{window.__hits++;"
        "document.getElementById('b').textContent='clicked';});</script>",
        encoding="utf-8")

    async def run():
        hub = webdebug.Hub()
        viewer = _Viewer()
        try:
            win = await hub.attach("w-real1", "s1", viewer)
            await hub.navigate(win, page.as_uri())
            for _ in range(100):
                if viewer.binary:
                    break
                await asyncio.sleep(0.1)
            assert viewer.binary, "no screencast frame arrived"
            assert viewer.binary[-1][:2] == b"\xff\xd8"  # JPEG

            snap = await hub.command("s1", "snapshot", {}, window=win.id)
            assert "button" in snap["value"]["elements"]

            rect = await hub.agent(win, "rect", {"selector": "#b"})
            box = rect["rect"]
            x = box["x"] + box["width"] / 2
            y = box["y"] + box["height"] / 2
            for kind, extra in (("mousePressed", {"buttons": 1}), ("mouseReleased", {"buttons": 0})):
                await hub.input_event(win, {"kind": "mouse", "type": kind, "x": x, "y": y,
                                            "button": "left", "clickCount": 1, **extra})
            assert await hub.evaluate(win, "window.__hits") == 1

            shot = await hub.command("s1", "screenshot", {}, window=win.id)
            assert Path(shot["value"]["image"]).read_bytes()[:8] == b"\x89PNG\r\n\x1a\n"
        finally:
            await hub.stop_all()

    asyncio.run(run())


def test_the_socket_carries_the_screen_and_the_users_typing(tmp_path, monkeypatch):
    """The path the user reported broken: open the page, watch its screen,
    type into it. Real browser, real WebSocket, no stub in between."""
    try:
        webdebug.find_browser()
    except webdebug.WebDebugError:
        pytest.skip("no Chromium on this machine for the web debug page")
    monkeypatch.setattr(webdebug, "image_folder", lambda: tmp_path)
    from urllib.parse import quote

    from aiohttp import WSMsgType

    page = tmp_path / "form.html"
    page.write_text(
        "<!doctype html><meta charset=utf-8><title>form</title>"
        "<input id=i><script>window.__typed=0;"
        "document.getElementById('i').addEventListener('input',e=>{window.__typed=e.target.value;});"
        "</script>", encoding="utf-8")

    async def run():
        client = TestClient(TestServer(_app(_Manager("s1"))))
        await client.start_server()
        try:
            ws = await client.ws_connect(
                "/api/webdebug/ws?window=w-abcd&session=s1&url=" + quote(page.as_uri(), safe=""))
            state = None
            frame = None
            for _ in range(200):
                msg = await ws.receive(timeout=10)
                if msg.type == WSMsgType.BINARY:
                    frame = msg.data
                    break
                if msg.type == WSMsgType.TEXT:
                    state = json.loads(msg.data)
            assert frame is not None and frame[:2] == b"\xff\xd8"
            assert state and state["type"] == "state"

            hub = client.server.app["webdebug"]
            await hub.command("s1", "click", {"selector": "#i"}, window="w-abcd")
            await ws.send_json({"type": "input", "event": {"kind": "text", "text": "hello"}})
            for _ in range(50):
                if await hub.evaluate(hub.window_for("s1", "w-abcd"),
                                      "document.getElementById('i').value") == "hello":
                    break
                await asyncio.sleep(0.1)
            win = hub.window_for("s1", "w-abcd")
            assert await hub.evaluate(win, "document.getElementById('i').value") == "hello"
            await ws.close()
        finally:
            await client.close()

    asyncio.run(run())


# --------------------------------------------------------------------------- #
# the MCP tools
# --------------------------------------------------------------------------- #
class _Client:
    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(("GET", path, None))
        return {"windows": []}

    def post(self, path, body, timeout=None):
        self.calls.append(("POST", path, body))
        return {"ok": True}


def test_the_tools_carry_the_session_and_the_op(monkeypatch):
    from claude_launcher import webdebug_mcp

    client = _Client()
    monkeypatch.setattr(webdebug_mcp, "_client", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s1")

    assert webdebug_mcp.call_tool("webdebug_windows", {}) == {"windows": []}
    assert client.calls[-1] == ("GET", "/api/webdebug/windows?session=s1", None)

    webdebug_mcp.call_tool("webdebug_click", {"ref": "e3"})
    method, path, body = client.calls[-1]
    assert (method, path) == ("POST", "/api/webdebug/command")
    assert body == {"session": "s1", "op": "click", "args": {"ref": "e3"}, "timeout": 20}

    webdebug_mcp.call_tool("webdebug_navigate", {"url": "http://x/", "window": "w-abcd"})
    _method, _path, body = client.calls[-1]
    assert body["op"] == "navigate" and body["window"] == "w-abcd" and body["timeout"] == 30
