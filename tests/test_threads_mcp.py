"""MCP tools for session threads (threads_mcp.py, claunch-doj98): every call
is the calling session, and reads name it as the reader."""

from urllib.parse import parse_qs, urlsplit

import pytest

from claude_launcher import prompt_packs, mcp_server, threads_mcp


class Client:
    def __init__(self):
        self.calls = []

    def get(self, path):
        self.calls.append(("GET", path))
        return {"threads": []}

    def post(self, path, body):
        self.calls.append(("POST", path, body))
        return {"thread": {"id": "th-1"}}


def test_tools_are_registered():
    names = {t["name"] for t in mcp_server.TOOLS}
    assert {"thread_list", "thread_read", "thread_reply", "thread_open", "thread_close"} <= names
    assert "[claunch thread" in threads_mcp.INSTRUCTIONS


def test_the_reply_format_is_markdown():
    """The agent is told the pane renders markdown, in the mcp-threads pack
    and on both tools that write text (claunch-5usj6); the server
    instructions point at that pack (claunch-8do04)."""
    assert "GitHub-flavored markdown" in prompt_packs.shipped("mcp-threads").body
    assert "markdown" in threads_mcp.INSTRUCTIONS
    assert "'mcp-threads'" in threads_mcp.INSTRUCTIONS
    tools = {t["name"]: t for t in threads_mcp.TOOLS}
    assert "markdown" in tools["thread_reply"]["description"]
    for name in ("thread_reply", "thread_open"):
        assert "markdown" in tools[name]["inputSchema"]["properties"]["text"]["description"]


def test_calls_carry_the_session(monkeypatch):
    client = Client()
    monkeypatch.setattr(threads_mcp, "_client", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    threads_mcp.call_tool("thread_list", {})
    assert parse_qs(urlsplit(client.calls[-1][1]).query) == {"session": ["s7"], "closed": ["0"]}
    threads_mcp.call_tool("thread_list", {"closed": True})
    assert parse_qs(urlsplit(client.calls[-1][1]).query)["closed"] == ["1"]
    threads_mcp.call_tool("thread_read", {"thread_id": "th-1", "after": 4})
    path = urlsplit(client.calls[-1][1])
    assert path.path == "/api/threads/th-1" and parse_qs(path.query) == {"reader": ["s7"], "after": ["4"]}
    threads_mcp.call_tool("thread_reply", {"thread_id": "th-1", "text": "done"})
    assert client.calls[-1] == ("POST", "/api/threads/th-1/messages", {"session": "s7", "text": "done"})
    threads_mcp.call_tool("thread_open", {"title": "side", "text": "hi"})
    assert client.calls[-1] == ("POST", "/api/threads", {"session": "s7", "title": "side", "text": "hi"})
    threads_mcp.call_tool("thread_close", {"thread_id": "th-1"})
    assert client.calls[-1] == ("POST", "/api/threads/th-1/close", {"session": "s7"})


def test_needs_a_session_and_a_thread_id(monkeypatch):
    monkeypatch.delenv("CLAUNCH_SESSION", raising=False)
    with pytest.raises(threads_mcp.ThreadMcpError, match="CLAUNCH_SESSION"):
        threads_mcp.call_tool("thread_list", {})
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    monkeypatch.setattr(threads_mcp, "_client", lambda: Client())
    with pytest.raises(threads_mcp.ThreadMcpError, match="thread_id"):
        threads_mcp.call_tool("thread_reply", {"text": "x"})


# -- images on the agent's message (claunch-euaz6) ---------------------------

PNG = bytes.fromhex("89504e470d0a1a0a") + b"a pretend png body"
JPEG = b"\xff\xd8\xff\xe0" + b"a pretend jpeg body"
WEBP = b"RIFF\x10\x00\x00\x00WEBPVP8 " + b"body"


class UploadClient(Client):
    def request(self, method, path, *, data=None, content_type=None):
        self.calls.append((method, path, content_type, data))
        return {"id": f"{len(self.calls):020x}.png", "bytes": len(data), "type": content_type}


def test_images_are_uploaded_then_named_on_the_message(monkeypatch, tmp_path):
    client = UploadClient()
    monkeypatch.setattr(threads_mcp, "_client", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    (tmp_path / "chart.png").write_bytes(PNG)
    (tmp_path / "photo.webp").write_bytes(JPEG)          # the type comes from the bytes, not the name
    (tmp_path / "w.bin").write_bytes(WEBP)

    threads_mcp.call_tool("thread_reply", {"thread_id": "th-1", "text": "see",
                                           "images": [str(tmp_path / "chart.png"), str(tmp_path / "photo.webp")]})
    assert client.calls[0] == ("POST", "/api/threads/images", "image/png", PNG)
    assert client.calls[1] == ("POST", "/api/threads/images", "image/jpeg", JPEG)
    assert client.calls[2] == ("POST", "/api/threads/th-1/messages",
                               {"session": "s7", "text": "see", "images": [f"{1:020x}.png", f"{2:020x}.png"]})

    client.calls.clear()
    threads_mcp.call_tool("thread_reply", {"thread_id": "th-1", "images": [str(tmp_path / "w.bin")]})
    assert client.calls[0][2] == "image/webp"
    assert client.calls[1] == ("POST", "/api/threads/th-1/messages",
                               {"session": "s7", "text": "", "images": [f"{1:020x}.png"]})

    client.calls.clear()
    threads_mcp.call_tool("thread_open", {"title": "Chart", "images": [str(tmp_path / "chart.png")]})
    assert client.calls[1] == ("POST", "/api/threads",
                               {"session": "s7", "title": "Chart", "images": [f"{1:020x}.png"]})


def test_bad_images_post_nothing(monkeypatch, tmp_path):
    """Every file is checked before the first upload: a bad one refuses the
    call and nothing reaches the daemon."""
    client = UploadClient()
    monkeypatch.setattr(threads_mcp, "_client", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    good = tmp_path / "ok.png"
    good.write_bytes(PNG)
    (tmp_path / "notes.txt").write_text("hello")
    cases = [
        ([str(good), str(tmp_path / "missing.png")], "cannot read"),
        ([str(good), str(tmp_path / "notes.txt")], "not a PNG, JPEG, GIF or WebP"),
        ([str(tmp_path)], "not a file"),
        ([str(good)] * (threads_mcp.MAX_IMAGES + 1), "at most"),
        ("ok.png", "list of image file paths"),
    ]
    for images, why in cases:
        with pytest.raises(threads_mcp.ThreadMcpError, match=why):
            threads_mcp.call_tool("thread_reply", {"thread_id": "th-1", "text": "x", "images": images})
    with pytest.raises(threads_mcp.ThreadMcpError, match="text or images"):
        threads_mcp.call_tool("thread_reply", {"thread_id": "th-1"})
    monkeypatch.setattr(threads_mcp, "MAX_IMAGE_BYTES", len(PNG) - 1)
    with pytest.raises(threads_mcp.ThreadMcpError, match="larger than"):
        threads_mcp.call_tool("thread_open", {"title": "t", "images": [str(good)]})
    assert client.calls == []


def test_the_tools_and_the_pack_name_images():
    tools = {t["name"]: t for t in threads_mcp.TOOLS}
    for name in ("thread_reply", "thread_open"):
        assert tools[name]["inputSchema"]["properties"]["images"]["type"] == "array"
    assert tools["thread_reply"]["inputSchema"]["required"] == ["thread_id"]
    assert "`images`" in prompt_packs.shipped("mcp-threads").body


def test_an_image_goes_through_the_real_upload_route(monkeypatch, tmp_path):
    """End to end over HTTP: the daemon client sends the file as a raw body
    under its media type, the staging route takes it, and the post keeps it
    with the thread on the agent's message."""
    import asyncio
    import threading

    from aiohttp import web

    from claude_launcher import daemon_client
    from claude_launcher.daemon import api, thread_images, threads_api
    from claude_launcher.threads import store as S

    class Manager:
        def get(self, name):
            if name != "s7":
                raise KeyError(name)
            return object()

    app = web.Application()
    app["manager"] = Manager()
    threads = threads_api.install(app, S.ThreadStore(tmp_path / "threads.sqlite3"))
    app.router.add_post("/api/threads/images", api.h_opening_image_upload)

    loop = asyncio.new_event_loop()
    runner = web.AppRunner(app)
    loop.run_until_complete(runner.setup())
    site = web.TCPSite(runner, "127.0.0.1", 0)
    loop.run_until_complete(site.start())
    port = site._server.sockets[0].getsockname()[1]
    t = threading.Thread(target=loop.run_forever, daemon=True)
    t.start()
    try:
        client = daemon_client.DaemonClient(f"http://127.0.0.1:{port}", "tok", actor="")
        monkeypatch.setattr(threads_mcp, "_client", lambda: client)
        monkeypatch.setenv("CLAUNCH_SESSION", "s7")
        (tmp_path / "chart.png").write_bytes(PNG)
        res = threads_mcp.call_tool("thread_open", {"title": "Chart", "text": "here",
                                                    "images": [str(tmp_path / "chart.png")]})
        img = res["message"]["images"][0]
        assert res["message"]["author"] == "agent" and img["type"] == "image/png"
        assert (thread_images.folder() / img["name"]).read_bytes() == PNG
        assert threads.has_image(res["thread"]["id"], img["name"])
    finally:
        asyncio.run_coroutine_threadsafe(runner.cleanup(), loop).result(10)
        loop.call_soon_threadsafe(loop.stop)
        t.join(10)
        loop.close()


def test_invite_and_uninvite_carry_the_session(monkeypatch):
    """claunch-p9wes: thread_invite names the guest; thread_uninvite removes
    one, or without a session leaves the thread as this session."""
    client = Client()
    monkeypatch.setattr(threads_mcp, "_client", lambda: client)
    monkeypatch.setenv("CLAUNCH_SESSION", "s7")
    names = {t["name"] for t in mcp_server.TOOLS}
    assert {"thread_invite", "thread_uninvite"} <= names
    threads_mcp.call_tool("thread_invite", {"thread_id": "th-1", "session": " s8 "})
    assert client.calls[-1] == ("POST", "/api/threads/th-1/guests", {"session": "s7", "guest": "s8"})
    threads_mcp.call_tool("thread_uninvite", {"thread_id": "th-1", "session": "s8"})
    assert client.calls[-1] == ("POST", "/api/threads/th-1/guests/s8/remove", {"session": "s7"})
    threads_mcp.call_tool("thread_uninvite", {"thread_id": "th-1"})
    assert client.calls[-1] == ("POST", "/api/threads/th-1/guests/s7/remove", {"session": "s7"})
    with pytest.raises(threads_mcp.ThreadMcpError, match="session is required"):
        threads_mcp.call_tool("thread_invite", {"thread_id": "th-1"})
    tools = {t["name"]: t for t in threads_mcp.TOOLS}
    assert "guest_threads" in tools["thread_list"]["description"]
    assert "QA tools" in tools["thread_invite"]["description"]
    body = prompt_packs.shipped("mcp-threads").body
    assert "thread_invite" in body and "guest_threads" in body and "reviewer and advisor" in body
