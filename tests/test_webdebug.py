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
import re
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


def test_the_browser_tab_sends_the_text_an_editing_key_produces():
    """Backspace and Enter only act on the page when Chrome is told what they
    produce; the tab therefore carries text for them too (s1262-rev)."""
    script = (STATIC / "webdebug.js").read_text(encoding="utf-8")
    assert "EDIT_TEXT" in script
    for key, text in (("Backspace", r'"\b"'), ("Enter", r'"\r"')):
        assert re.search(rf'{key}:\s*"{re.escape(text.strip(chr(34)))}"', script), key


def test_a_drag_and_a_right_click_keep_their_button():
    script = (STATIC / "webdebug.js").read_text(encoding="utf-8")
    assert "buttonName(e.button)" in script and "buttons: e.buttons" in script
    assert 'button: "left"' not in script


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


def test_the_refusal_names_what_is_accepted(monkeypatch):
    monkeypatch.delenv(webdebug.NO_SANDBOX_ENV, raising=False)
    with pytest.raises(webdebug.WebDebugError) as caught:
        webdebug.target_url("javascript:alert(1)")
    assert "javascript" in str(caught.value) and "file" in str(caught.value)


def test_the_browser_keeps_chrome_s_sandbox_unless_the_operator_turns_it_off(monkeypatch):
    """This browser renders whatever page someone names, so its renderer runs
    sandboxed; the flag is dropped only where the sandbox cannot work."""
    monkeypatch.delenv(webdebug.NO_SANDBOX_ENV, raising=False)
    monkeypatch.setattr(webdebug.os, "geteuid", lambda: 1000, raising=False)
    args = webdebug.browser_args("/usr/bin/chrome", "/profile")
    assert "--no-sandbox" not in args
    assert args[0] == "/usr/bin/chrome" and args[-1] == "about:blank"

    monkeypatch.setenv(webdebug.NO_SANDBOX_ENV, "1")
    assert "--no-sandbox" in webdebug.browser_args("/usr/bin/chrome", "/profile")

    # root cannot use Chrome's sandbox at all
    monkeypatch.delenv(webdebug.NO_SANDBOX_ENV)
    monkeypatch.setattr(webdebug.os, "geteuid", lambda: 0, raising=False)
    assert "--no-sandbox" in webdebug.browser_args("/usr/bin/chrome", "/profile")


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
        self.pages = []
        self.tls = []

    @property
    def live(self):
        return True

    async def ensure(self):
        return self.cdp

    async def new_page(self, url="about:blank", *, insecure_tls=False):
        page = {"context_id": "ctx-1", "target_id": "tgt-1", "session_id": f"sess-{len(self.pages) + 1}"}
        self.pages.append({**page, "insecure_tls": insecure_tls})
        self.tls.append((page["session_id"], insecure_tls))
        return page

    async def set_insecure_tls(self, session_id, value):
        self.tls.append((session_id, bool(value)))
        self.cdp.sent.append(("Security.setIgnoreCertificateErrors",
                              {"ignore": bool(value)}, session_id))

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


def test_a_scrolled_element_is_captured_where_it_is(tmp_path, monkeypatch):
    """The clip of an element screenshot is in page coordinates while the
    agent's rect is viewport-relative; without the offset a scrolled element
    captured the empty region above it (s1236-rev2, s511-rev)."""
    try:
        webdebug.find_browser()
    except webdebug.WebDebugError:
        pytest.skip("no Chromium on this machine for the web debug page")
    monkeypatch.setattr(webdebug, "image_folder", lambda: tmp_path)
    page = tmp_path / "scrolled.html"
    page.write_text(
        "<!doctype html><meta charset=utf-8><title>scrolled</title><style>"
        "html,body{margin:0;padding:0}body{height:5000px}"
        ".box{position:absolute;left:0;width:120px;height:120px;background:#ff0000}"
        "#top{top:0}#deep{top:2000px}</style>"
        "<div id=top class=box></div><div id=deep class=box></div>", encoding="utf-8")

    async def run():
        hub = webdebug.Hub()
        try:
            win = await hub.attach("w-scrl", "s1", _Viewer())
            await hub.navigate(win, page.as_uri())
            near = await hub.command("s1", "screenshot", {"selector": "#top"}, window="w-scrl")
            far = await hub.command("s1", "screenshot", {"selector": "#deep"}, window="w-scrl")
            info = await hub.agent(win, "rect", {"selector": "#deep"})
            # the element is deep in the document but on screen once scrolled
            assert info["page"]["y"] >= 1500 and info["rect"]["y"] < 800
            # two identical red squares: the captures must show the same thing
            assert (Path(far["value"]["image"]).read_bytes()
                    == Path(near["value"]["image"]).read_bytes())
        finally:
            await hub.stop_all()

    asyncio.run(run())


def test_the_agent_installs_itself_into_a_document_that_predates_it(tmp_path, monkeypatch):
    """The first document (about:blank) exists before the agent is injected;
    a command there must install it and answer, not return null."""
    try:
        webdebug.find_browser()
    except webdebug.WebDebugError:
        pytest.skip("no Chromium on this machine for the web debug page")
    monkeypatch.setattr(webdebug, "image_folder", lambda: tmp_path)

    async def run():
        hub = webdebug.Hub()
        try:
            win = await hub.attach("w-fresh", "s1", _Viewer())
            value = await hub.agent(win, "where", {})
            assert isinstance(value, dict) and value.get("url") == "about:blank"
        finally:
            await hub.stop_all()

    asyncio.run(run())


def test_the_socket_carries_a_backspace_and_an_enter(tmp_path, monkeypatch):
    """Keys that edit rather than print only act when Chrome is told what they
    produce; without that text the page keeps the character and never submits
    (s1262-rev measured both)."""
    try:
        webdebug.find_browser()
    except webdebug.WebDebugError:
        pytest.skip("no Chromium on this machine for the web debug page")
    monkeypatch.setattr(webdebug, "image_folder", lambda: tmp_path)
    from urllib.parse import quote

    from aiohttp import WSMsgType

    page = tmp_path / "keys.html"
    page.write_text(
        "<!doctype html><meta charset=utf-8><title>keys</title>"
        "<form id=f><input id=i></form><script>window.__submits=0;"
        "document.getElementById('f').addEventListener('submit',e=>{e.preventDefault();"
        "window.__submits++;});</script>", encoding="utf-8")

    async def run():
        client = TestClient(TestServer(_app(_Manager("s1"))))
        await client.start_server()
        try:
            ws = await client.ws_connect(
                "/api/webdebug/ws?window=w-keys&session=s1&url=" + quote(page.as_uri(), safe=""))
            for _ in range(200):
                msg = await ws.receive(timeout=10)
                if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "state":
                    break
            hub = client.server.app["webdebug"]
            await hub.command("s1", "click", {"selector": "#i"}, window="w-keys")
            await ws.send_json({"type": "input", "event": {"kind": "text", "text": "abc"}})
            for key, code, vk, text in (("Backspace", "Backspace", 8, "\b"),
                                        ("Enter", "Enter", 13, "\r")):
                await ws.send_json({"type": "input", "event": {
                    "kind": "key", "type": "keyDown", "key": key, "code": code,
                    "keyCode": vk, "text": text}})
            win = hub.window_for("s1", "w-keys")
            for _ in range(50):
                if await hub.evaluate(win, "window.__submits") == 1:
                    break
                await asyncio.sleep(0.1)
            assert await hub.evaluate(win, "document.getElementById('i').value") == "ab"
            assert await hub.evaluate(win, "window.__submits") == 1
            await ws.close()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_bad_value_from_the_viewer_is_an_error_and_not_the_end_of_the_socket():
    async def run():
        hub = webdebug.Hub()
        hub.browser = _FakeBrowser()
        try:
            viewer = _Viewer()
            win = await hub.attach("w-abcd", "s1", viewer)
            await webdebug._viewer_message(hub, win, {"type": "quality", "quality": "high"})
            assert any(m.get("type") == "error" for m in viewer.json)
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


def _tab_move_source():
    """What the tab's move does with the button, read from the shipped source:
    ("table", <table source>) when it names the held button through the table,
    ("none", ...) when it names no button at all, ("e.button", ...) when it
    reads the move's own e.button. The table source is returned so the browser
    test can run the shipped mapping instead of a copy."""
    script = (STATIC / "webdebug.js").read_text(encoding="utf-8")
    start = script.index('screen.addEventListener("mousemove"')
    end = script.index("screen.addEventListener(", start + 1)
    move = script[start:end]
    assert 'type: "mouseMoved"' in move and "buttons: e.buttons" in move
    table = script[script.index("const HELD_BUTTONS"):
                   script.index("}", script.index("function heldButton(")) + 1]
    assert "function heldButton" in table and 'return "none"' in table
    if "heldButton(e.buttons)" in move:
        return "table", table
    if 'button: "none"' in move:
        return "none", table
    if "buttonName(e.button)" in move:
        return "e.button", table
    return "unknown", table


def test_a_hover_reaches_the_page_as_no_button_held(tmp_path, monkeypatch):
    """The two halves of one field. Chrome takes the page's e.buttons from a
    move's `button`, so naming a button with an empty mask made every hover a
    drag (s490-rev). It also takes its own drag gesture from there, so naming
    no button while one is held kills the drag -- a text selection never starts
    (s648-rev measured range 0 against 66). The move must name the held button
    and nothing when none is held."""
    try:
        webdebug.find_browser()
    except webdebug.WebDebugError:
        pytest.skip("no Chromium on this machine for the web debug page")
    monkeypatch.setattr(webdebug, "image_folder", lambda: tmp_path)
    from urllib.parse import quote

    from aiohttp import WSMsgType

    page = tmp_path / "moves.html"
    page.write_text(
        "<!doctype html><meta charset=utf-8><title>moves</title>"
        "<body style='margin:0;font:16px monospace'><p id=t>hello world selection</p>"
        "<script>window.__moves=[];document.addEventListener('mousemove',"
        "e=>{window.__moves.push({button:e.button,buttons:e.buttons});});</script>",
        encoding="utf-8")

    async def run():
        client = TestClient(TestServer(_app(_Manager("s1"))))
        await client.start_server()
        try:
            ws = await client.ws_connect(
                "/api/webdebug/ws?window=w-moves&session=s1&url=" + quote(page.as_uri(), safe=""))
            for _ in range(200):
                msg = await ws.receive(timeout=10)
                if msg.type == WSMsgType.TEXT and json.loads(msg.data).get("type") == "state":
                    break
            hub = client.server.app["webdebug"]
            win = hub.window_for("s1", "w-moves")
            # the state message goes out at attach, before the navigation lands
            for _ in range(100):
                if await hub.evaluate(win, "document.title") == "moves":
                    break
                await asyncio.sleep(0.1)
            else:
                raise AssertionError("the page never loaded")

            async def move(button, buttons, want):
                await ws.send_json({"type": "input", "event": {
                    "kind": "mouse", "type": "mouseMoved", "x": 10, "y": 10,
                    "button": button, "buttons": buttons}})
                for _ in range(50):
                    seen = json.loads(await hub.evaluate(win, "JSON.stringify(window.__moves)"))
                    if seen and len(seen) == want:
                        return seen[-1]
                    await asyncio.sleep(0.1)
                raise AssertionError(
                    f"no move reached the page for button={button!r} buttons={buttons}: {seen} "
                    f"at {await hub.evaluate(win, 'location.href')}")

            # what the tab puts on a move of this mask, taken from its own rule
            shape, table = _tab_move_source()

            async def tab_button(mask):
                if shape == "table":
                    return await hub.evaluate(
                        win, "(() => {" + table + f"\nreturn heldButton({mask}); }})()")
                if shape == "none":
                    return "none"   # names no button whatever is held
                if shape == "e.button":
                    return "left"   # a move's own e.button is 0, which is left
                raise AssertionError(f"the move's button shape is {shape!r}")

            assert await tab_button(0) == "none"

            # a hover names nothing and the page sees no button held
            assert await move(await tab_button(0), 0, 1) == {"button": 0, "buttons": 0}

            # naming a button out of an empty mask fabricates a press
            assert await move("left", 0, 2) == {"button": 0, "buttons": 1}

            # a drag keeps the mask and the name, and the page selects text
            box = (await hub.agent(win, "rect", {"selector": "#t"}))["rect"]
            y = box["y"] + box["height"] / 2
            left, right = box["x"] + 2, box["x"] + box["width"] - 2

            async def drag(name):
                await hub.evaluate(win, "getSelection().removeAllRanges()")
                await ws.send_json({"type": "input", "event": {
                    "kind": "mouse", "type": "mousePressed", "x": left, "y": y,
                    "button": "left", "buttons": 1, "clickCount": 1}})
                for frac in (0.3, 0.6, 1.0):
                    await ws.send_json({"type": "input", "event": {
                        "kind": "mouse", "type": "mouseMoved",
                        "x": left + (right - left) * frac, "y": y,
                        "button": name, "buttons": 1}})
                    await asyncio.sleep(0.05)
                for _ in range(50):
                    seen = await hub.evaluate(
                        win, "JSON.stringify({type: getSelection().type, "
                             "text: String(getSelection())})")
                    if json.loads(seen)["type"] != "None":
                        break
                    await asyncio.sleep(0.1)
                await ws.send_json({"type": "input", "event": {
                    "kind": "mouse", "type": "mouseReleased", "x": right, "y": y,
                    "button": "left", "buttons": 0, "clickCount": 1}})
                return json.loads(seen)

            selected = await drag(await tab_button(1))
            assert selected["type"] == "Range" and "hello world" in selected["text"], selected

            # the shape this fix had before: the mask is right, the drag is dead
            stalled = await drag("none")
            assert stalled == {"type": "Caret", "text": ""}, stalled

            # the table names each held button, and nothing when none is held
            assert [await tab_button(m) for m in (0, 1, 2, 4)] == ["none", "left", "right", "middle"]
            await ws.close()
        finally:
            await client.close()

    asyncio.run(run())


def test_a_move_names_the_button_that_is_held():
    """The tab builds the move itself: the name comes from the mask, never from
    the move's own e.button (which is always 0)."""
    shape, table = _tab_move_source()
    assert shape == "table", f"the move takes its button from {shape!r}"
    assert "e.button" not in table


def test_the_top_bar_switch_is_part_of_the_window_s_dial():
    """The page half of the per-window certificate answer: the switch is in the
    top bar, it goes into the socket's query (so a reload keeps it), it is sent
    as a message while the socket is up, and the daemon's state message is what
    the page ends up showing."""
    html = (STATIC / "webdebug.html").read_text(encoding="utf-8")
    script = (STATIC / "webdebug.js").read_text(encoding="utf-8")
    assert 'id="wd-tls"' in html and "인증서 무시" in html
    assert 'q.set("insecure", "1")' in script
    assert 'send({ type: "insecure", insecure: insecureTls })' in script
    assert "msg.insecure_tls" in script


def test_an_absent_switch_in_the_query_is_not_off():
    """A query without the switch means the caller did not choose, so the window
    takes the configured default; ``insecure=0`` is a choice, and it is off."""
    assert webdebug._flag(None) is None
    assert webdebug._flag("") is None
    assert webdebug._flag("  ") is None
    assert webdebug._flag("1") is True
    assert webdebug._flag("true") is True
    assert webdebug._flag("0") is False
    assert webdebug._flag("false") is False
    assert webdebug._flag("off") is False


# --------------------------------------------------------------------------- #
# the three knobs: config keys now, environment variables still winning
# (claunch-o8pb7.2.1)
# --------------------------------------------------------------------------- #
def _no_env(monkeypatch):
    for name in (webdebug.MAX_WINDOWS_ENV, webdebug.NO_SANDBOX_ENV,
                 webdebug.INSECURE_TLS_ENV):
        monkeypatch.delenv(name, raising=False)


def test_the_three_keys_are_daemon_settings_with_their_old_defaults(home):
    """(2) The three keys exist in DAEMON_DEFAULTS, and their built-in defaults
    are what the module did when the variables were all that existed."""
    from claude_launcher import store

    for key in ("webdebug_max_windows", "webdebug_no_sandbox", "webdebug_insecure_tls"):
        assert key in store.DAEMON_DEFAULTS, key
    assert store.DAEMON_DEFAULTS["webdebug_max_windows"] == 1
    assert store.DAEMON_DEFAULTS["webdebug_no_sandbox"] is False
    assert store.DAEMON_DEFAULTS["webdebug_insecure_tls"] is False


def test_the_page_is_told_the_certificate_answer_before_it_loads(home, monkeypatch):
    """The real Browser.new_page path, not the hub's: the answer is sent to the
    new page's own CDP session, and before anything is loaded there."""
    _no_env(monkeypatch)
    monkeypatch.setattr(webdebug, "AGENT_FILE", Path("/nonexistent/webdebug-agent.js"))

    async def run():
        browser = webdebug.Browser(lambda event: None)
        fake = _FakeCDP()
        browser._cdp = fake
        page = await browser.new_page(insecure_tls=True)
        tls = [i for i, (method, _p, _s) in enumerate(fake.sent)
               if method == "Security.setIgnoreCertificateErrors"]
        assert tls, "the page's CDP session was never told"
        assert fake.sent[tls[0]] == ("Security.setIgnoreCertificateErrors",
                                     {"ignore": True}, page["session_id"])
        # before the page is given anything to run
        order = [method for method, _p, _s in fake.sent]
        assert order.index("Security.setIgnoreCertificateErrors") < order.index(
            "Emulation.setDeviceMetricsOverride")
        assert "Page.startScreencast" not in order

        fake.sent.clear()
        await browser.new_page()
        assert [entry for entry in fake.sent
                if entry[0] == "Security.setIgnoreCertificateErrors"] == [
            ("Security.setIgnoreCertificateErrors", {"ignore": False}, "sess-1")]

    asyncio.run(run())


def test_the_saved_keys_are_what_a_window_uses(home, monkeypatch):
    """(1) A value written as a config key reaches the window-creation path --
    not just the reader, but the page that is actually created."""
    from claude_launcher import store

    _no_env(monkeypatch)
    monkeypatch.setattr(webdebug.os, "geteuid", lambda: 1000, raising=False)
    store.set_daemon_field("webdebug_max_windows", 3)
    store.set_daemon_field("webdebug_no_sandbox", True)
    store.set_daemon_field("webdebug_insecure_tls", True)
    assert webdebug.max_windows() == 3
    assert "--no-sandbox" in webdebug.browser_args("/usr/bin/chrome", "/profile")
    assert webdebug.insecure_tls_default() is True

    async def run():
        hub = webdebug.Hub()
        hub.browser = _FakeBrowser()
        try:
            await hub.attach("w-aaaa", "s1", _Viewer())
            # the page was created with the configured default
            assert hub.browser.pages[0]["insecure_tls"] is True
            # max_windows 3 means two more fit without closing the first
            await hub.attach("w-bbbb", "s1", _Viewer())
            await hub.attach("w-cccc", "s1", _Viewer())
            assert sorted(hub.windows) == ["w-aaaa", "w-bbbb", "w-cccc"]
            await hub.attach("w-dddd", "s1", _Viewer())
            assert sorted(hub.windows) == ["w-bbbb", "w-cccc", "w-dddd"]
        finally:
            await hub.stop_all()

    asyncio.run(run())


def test_the_environment_variables_still_win(home, monkeypatch):
    """(3) Regression: an operator who only set the variables sees exactly what
    the module did before the keys existed."""
    from claude_launcher import store

    monkeypatch.setattr(webdebug.os, "geteuid", lambda: 1000, raising=False)
    store.set_daemon_field("webdebug_max_windows", 5)
    store.set_daemon_field("webdebug_no_sandbox", False)
    store.set_daemon_field("webdebug_insecure_tls", False)

    monkeypatch.setenv(webdebug.MAX_WINDOWS_ENV, "2")
    monkeypatch.setenv(webdebug.NO_SANDBOX_ENV, "1")
    monkeypatch.setenv(webdebug.INSECURE_TLS_ENV, "1")
    assert webdebug.max_windows() == 2
    assert webdebug.sandbox_off() is True
    assert webdebug.insecure_tls_default() is True
    assert "--no-sandbox" in webdebug.browser_args("/usr/bin/chrome", "/profile")
    assert "--ignore-certificate-errors" in webdebug.browser_args("/usr/bin/chrome", "/profile")

    # an empty value is how a shell spells "nothing here", not a request for zero
    monkeypatch.setenv(webdebug.MAX_WINDOWS_ENV, "")
    assert webdebug.max_windows() == 5

    # the variable off (0) beats a key that is on
    store.set_daemon_field("webdebug_insecure_tls", True)
    monkeypatch.setenv(webdebug.INSECURE_TLS_ENV, "0")
    assert webdebug.insecure_tls_default() is False


def test_the_browser_flag_is_the_environment_s_and_the_key_is_per_window(home, monkeypatch):
    """The browser flag --ignore-certificate-errors covers every window and no
    window can narrow it, so only the environment sets it. The key is the
    default for a new window, applied to that window's own page."""
    from claude_launcher import store

    _no_env(monkeypatch)
    monkeypatch.setattr(webdebug.os, "geteuid", lambda: 1000, raising=False)
    store.set_daemon_field("webdebug_insecure_tls", True)
    assert "--ignore-certificate-errors" not in webdebug.browser_args("/usr/bin/chrome", "/p")
    monkeypatch.setenv(webdebug.INSECURE_TLS_ENV, "1")
    assert "--ignore-certificate-errors" in webdebug.browser_args("/usr/bin/chrome", "/p")


def test_insecure_tls_is_one_window_s_answer(home, monkeypatch):
    """(4) Two windows, one on and one off: the flag goes to each window's own
    CDP session, and neither window's answer changes the other's."""
    _no_env(monkeypatch)
    monkeypatch.setattr(webdebug.os, "geteuid", lambda: 1000, raising=False)

    async def run():
        hub = webdebug.Hub()
        hub.browser = _FakeBrowser()
        viewers = [_Viewer(), _Viewer()]
        try:
            safe = await hub.attach("w-safe", "s1", viewers[0], insecure=False)
            loose = await hub.attach("w-loose", "s1", viewers[1], insecure=True)
            assert safe.insecure_tls is False and loose.insecure_tls is True
            assert [(s, v) for s, v in hub.browser.tls] == [("sess-1", False), ("sess-2", True)]
            # each window says its own answer, in the state the tab reads
            assert [m["insecure_tls"] for m in viewers[0].json if m.get("type") == "state"] == [False]
            assert [m["insecure_tls"] for m in viewers[1].json if m.get("type") == "state"] == [True]

            # a later viewer that states no choice leaves an existing window as
            # it is: absence is not "off"
            await hub.attach("w-loose", "s1", _Viewer())
            assert loose.insecure_tls is True

            # the top-bar switch flips one window, and only that one
            await webdebug._viewer_message(hub, loose, {"type": "insecure", "insecure": False})
            assert loose.insecure_tls is False and safe.insecure_tls is False
            assert hub.browser.tls[-1] == ("sess-2", False)
        finally:
            await hub.stop_all()

    asyncio.run(run())


def test_the_setting_route_reads_and_writes_the_three(home, monkeypatch):
    """(2) The Settings page's route: GET shows the stored values, the effective
    values and the environment that overrides them; PUT writes and null resets."""
    _no_env(monkeypatch)
    monkeypatch.setattr(webdebug.os, "geteuid", lambda: 1000, raising=False)

    async def run():
        client = TestClient(TestServer(_app(_Manager("s1"))))
        await client.start_server()
        try:
            body = await (await client.get("/api/webdebug-setting")).json()
            assert body["values"] == {"max_windows": 1, "no_sandbox": False,
                                      "insecure_tls": False}
            assert body["defaults"] == {"max_windows": 1, "no_sandbox": False,
                                        "insecure_tls": False}
            assert body["env"]["max_windows"] == {"name": webdebug.MAX_WINDOWS_ENV, "set": False}

            r = await client.put("/api/webdebug-setting",
                                 json={"max_windows": 3, "no_sandbox": True,
                                       "insecure_tls": True})
            body = await r.json()
            assert r.status == 200
            assert body["values"] == {"max_windows": 3, "no_sandbox": True,
                                      "insecure_tls": True}
            assert body["effective"] == body["values"]

            r = await client.put("/api/webdebug-setting", json={"max_windows": None})
            assert r.status == 200
            assert (await r.json())["values"]["max_windows"] == 1

            # the value the page stores is the value the daemon reads
            assert webdebug.max_windows() == 1
            assert webdebug.insecure_tls_default() is True

            for bad, why in (
                    ({"max_windows": 0}, "between"),
                    ({"max_windows": 99}, "between"),
                    ({"max_windows": 2.5}, "whole"),
                    ({"max_windows": True}, "whole"),
                    ({"no_sandbox": "yes"}, "true, false or null"),
                    ({}, "give")):
                r = await client.put("/api/webdebug-setting", json=bad)
                assert r.status == 400, (bad, await r.text())
                assert why in (await r.json())["error"]
        finally:
            await client.close()

    asyncio.run(run())


def test_the_route_reports_an_environment_that_overrides_the_saved_value(home, monkeypatch):
    """The operator who set the variable sees the page's value ignored; the
    route says which variable, so the page can say why."""
    _no_env(monkeypatch)
    monkeypatch.setattr(webdebug.os, "geteuid", lambda: 1000, raising=False)
    monkeypatch.setenv(webdebug.INSECURE_TLS_ENV, "1")

    async def run():
        client = TestClient(TestServer(_app(_Manager("s1"))))
        await client.start_server()
        try:
            body = await (await client.get("/api/webdebug-setting")).json()
            assert body["values"]["insecure_tls"] is False
            assert body["effective"]["insecure_tls"] is True
            assert body["env"]["insecure_tls"] == {"name": webdebug.INSECURE_TLS_ENV,
                                                   "set": True}
        finally:
            await client.close()

    asyncio.run(run())
