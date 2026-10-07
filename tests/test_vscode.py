"""VS Code in the browser (claunch-9omyu.1): settings, targets, install, the
daemon's server manager and its /vscode/<key>/ proxy.

The servers here are a stand-in for code-server: a small aiohttp app on the
unix socket the daemon names, which records the argv it was started with and
echoes back what reached it. That is what the proxy's contract is about --
which headers survive, which paths are refused, what gate stands in front --
and a real code-server would add a 220 MB download and nothing a test could
assert on. The relay case runs the same stand-in through a real psmux-relay
when one is built (see ``_relay_exe``).
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import io
import json
import subprocess
import sys
import tarfile
import textwrap
import time
from pathlib import Path

import pytest
from aiohttp import WSMsgType, web
from aiohttp.test_utils import TestClient, TestServer

from claude_launcher import vscode as vs
from claude_launcher import workspaces
from claude_launcher.daemon import vscode as dvs
from claude_launcher.daemon.api import build_app
from claude_launcher.daemon.manager import SessionManager
from claude_launcher.daemon.mesh import MeshManager

from _relay_exe import find_relay_exe

BEARER = {"Authorization": "Bearer sekrit"}

pytestmark = pytest.mark.skipif(sys.platform == "win32", reason="POSIX-only feature")


# --------------------------------------------------------------------------- #
# fixtures
# --------------------------------------------------------------------------- #
FAKE_SERVER = textwrap.dedent('''\
    import json, os, sys
    from aiohttp import web
    argv = sys.argv[1:]
    sock = argv[argv.index("--socket") + 1]
    data = argv[argv.index("--user-data-dir") + 1]
    with open(os.path.join(data, "argv.json"), "w") as fh:
        json.dump(argv, fh)

    async def healthz(request):
        return web.json_response({"status": "alive"})

    async def echo(request):
        if request.headers.get("Upgrade", "").lower() == "websocket":
            ws = web.WebSocketResponse()
            await ws.prepare(request)
            async for msg in ws:
                if msg.type == web.WSMsgType.TEXT:
                    await ws.send_str("echo:" + msg.data)
                elif msg.type == web.WSMsgType.BINARY:
                    await ws.send_bytes(msg.data[::-1])
            return ws
        body = await request.read()
        return web.json_response({
            "path": request.path_qs,
            "method": request.method,
            "headers": dict(request.headers),
            "body": body.decode(),
            "folder": argv[-1],
        })

    async def root(request):
        raise web.HTTPFound("./?folder=" + argv[-1])

    app = web.Application()
    app.router.add_get("/healthz", healthz)
    app.router.add_get("/", root)
    app.router.add_route("*", "/{tail:.+}", echo)
    web.run_app(app, path=sock, print=None)
''')


@pytest.fixture
def fake_cs(tmp_path):
    """An executable that behaves enough like code-server for the proxy."""
    script = tmp_path / "fake-code-server"
    script.write_text(f"#!{sys.executable}\n" + FAKE_SERVER, encoding="utf-8")
    script.chmod(0o755)
    vs.update_settings({"command": str(script)})
    return script


@pytest.fixture
def ws_tree(tmp_path):
    """A registered workspace with two linked-worktree checkouts under it."""
    root = tmp_path / "proj"
    for name in ("wt-a", "wt-b"):
        wt = root / ".claude" / "worktrees" / name
        wt.mkdir(parents=True)
        (wt / ".git").write_text("gitdir: elsewhere\n", encoding="utf-8")
    # Not a checkout: no .git file, so not a target.
    (root / ".claude" / "worktrees" / "stray").mkdir()
    (root / "src").mkdir()
    workspaces.add(str(root), name="proj")
    return root


def _app():
    mgr = SessionManager(idle_threshold=0.5, scrollback=200, restore_default=True)
    return build_app(mgr, "sekrit", started_at=time.monotonic(), mesh=MeshManager(mgr))


async def _client(app):
    client = TestClient(TestServer(app))
    await client.start_server()
    return client


# --------------------------------------------------------------------------- #
# settings
# --------------------------------------------------------------------------- #
def test_settings_defaults_and_update():
    assert vs.settings() == vs.DEFAULTS
    assert vs.DEFAULTS["port_proxy"] is False
    assert vs.DEFAULTS["idle_timeout"] == 1800.0
    out = vs.update_settings({"idle_timeout": 600, "port_proxy": True})
    assert out["idle_timeout"] == 600.0 and out["port_proxy"] is True
    assert vs.settings()["port_proxy"] is True


@pytest.mark.parametrize("changes", [
    {"idle_timeout": 5},
    {"idle_timeout": "soon"},
    {"enabled": "yes"},
    {"port_proxy": 1},
    {"nope": True},
    {"idle_timeout": 900, "enabled": "yes"},  # one bad value: nothing written
])
def test_settings_rejects_bad_values(changes):
    with pytest.raises(vs.VSCodeError):
        vs.update_settings(changes)
    assert vs.settings() == vs.DEFAULTS


# --------------------------------------------------------------------------- #
# targets
# --------------------------------------------------------------------------- #
def test_targets_root_and_each_worktree(ws_tree):
    keys = [t.key for t in vs.list_targets()]
    assert keys == ["proj", "proj~wt-a", "proj~wt-b"]
    wt = vs.workspace_target("proj", "wt-b")
    assert wt.kind == "worktree" and wt.folder.endswith("wt-b")
    with pytest.raises(vs.VSCodeError):
        vs.workspace_target("proj", "stray")
    with pytest.raises(vs.VSCodeError):
        vs.workspace_target("ghost")


def test_path_target_picks_worktree_root_or_adhoc(ws_tree, tmp_path):
    inside_wt = ws_tree / ".claude" / "worktrees" / "wt-a"
    (inside_wt / "pkg").mkdir()
    assert vs.path_target(str(inside_wt / "pkg")).key == "proj~wt-a"
    assert vs.path_target(str(ws_tree / "src")).key == "proj"
    other = tmp_path / "elsewhere"
    other.mkdir()
    adhoc = vs.path_target(str(other))
    assert adhoc.kind == "adhoc" and adhoc.key.startswith("adhoc-")
    assert adhoc.folder == str(other.resolve())
    # An ad-hoc key means nothing without the daemon that made it.
    assert vs.key_target(adhoc.key) is None
    assert vs.key_target("proj~wt-a").folder == str(inside_wt)
    assert vs.key_target("../etc") is None


def test_socket_path_stays_under_the_length_limit(monkeypatch, tmp_path):
    deep = tmp_path / ("d" * 120)
    monkeypatch.setenv("CLAUDE_LAUNCHER_HOME", str(deep))
    path = vs.socket_path("proj~wt-a")
    assert len(str(path)) <= 108
    assert vs.socket_path("proj~wt-b") != path


def test_windows_is_unsupported(monkeypatch):
    monkeypatch.setattr(vs.sys, "platform", "win32")
    assert vs.unsupported_reason()
    with pytest.raises(vs.VSCodeError):
        vs.install(fetch=lambda *a: None)


# --------------------------------------------------------------------------- #
# installing
# --------------------------------------------------------------------------- #
def _tarball(entries) -> bytes:
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name, kind, payload in entries:
            info = tarfile.TarInfo(name)
            if kind == "file":
                info.size = len(payload)
                info.mode = 0o755
                tar.addfile(info, io.BytesIO(payload))
            elif kind == "dir":
                info.type = tarfile.DIRTYPE
                info.mode = 0o755
                tar.addfile(info)
            elif kind == "symlink":
                info.type = tarfile.SYMTYPE
                info.linkname = payload
                tar.addfile(info)
    return buf.getvalue()


def _patch_asset(monkeypatch, blob: bytes) -> None:
    monkeypatch.setattr(vs, "asset_key", lambda: ("linux", "amd64"))
    monkeypatch.setattr(vs, "ASSETS", {("linux", "amd64"): hashlib.sha256(blob).hexdigest()})


def _fetcher(blob: bytes):
    def fetch(url, dest, progress):
        assert url.endswith(f"code-server-{vs.VERSION}-linux-amd64.tar.gz")
        dest.write_bytes(blob)
        progress(len(blob), len(blob))
    return fetch


def test_install_verifies_and_moves_into_place(monkeypatch):
    top = f"code-server-{vs.VERSION}-linux-amd64"
    blob = _tarball([
        (top, "dir", b""), (f"{top}/bin", "dir", b""),
        (f"{top}/bin/code-server", "file", b"#!/bin/sh\n"),
        (f"{top}/lib", "symlink", "bin"),
    ])
    _patch_asset(monkeypatch, blob)
    binary = vs.install(fetch=_fetcher(blob))
    assert binary == vs.install_dir() / "bin" / "code-server"
    assert vs.binary() == str(binary)
    assert not list((vs.root_dir() / "downloads").glob("*.part"))


def test_install_refuses_a_digest_mismatch(monkeypatch):
    blob = _tarball([("x/bin/code-server", "file", b"x")])
    _patch_asset(monkeypatch, blob)
    monkeypatch.setattr(vs, "ASSETS", {("linux", "amd64"): "0" * 64})
    with pytest.raises(vs.VSCodeError, match="sha256"):
        vs.install(fetch=_fetcher(blob))
    assert vs.installed_binary() is None


@pytest.mark.parametrize("entries", [
    [("../escape", "file", b"x")],
    [("x/bin/code-server", "file", b"x"), ("x/evil", "symlink", "/etc/passwd")],
    [("x/bin/code-server", "file", b"x"), ("x/up", "symlink", "../../..")],
])
def test_install_refuses_entries_outside_the_install(monkeypatch, entries):
    blob = _tarball(entries)
    _patch_asset(monkeypatch, blob)
    with pytest.raises(vs.VSCodeError, match="refusing"):
        vs.install(fetch=_fetcher(blob))
    assert vs.installed_binary() is None


# --------------------------------------------------------------------------- #
# the API and the proxy
# --------------------------------------------------------------------------- #
def test_vscode_paths_need_the_daemon_login(ws_tree):
    async def run():
        client = await _client(_app())
        try:
            for path in ("/vscode/proj/", "/vscode/proj/static/x.js", "/api/vscode"):
                resp = await client.get(path, allow_redirects=False)
                assert resp.status == 401, path
            page = await client.get("/vscode/proj/")
            assert "Log in" in await page.text()
            assert (await client.get("/api/vscode", headers=BEARER)).status == 200
        finally:
            await client.close()
    asyncio.run(run())


def test_open_without_install_asks_for_it(ws_tree):
    async def run():
        client = await _client(_app())
        try:
            resp = await client.post("/api/vscode/open", json={"workspace": "proj"}, headers=BEARER)
            assert resp.status == 409
            body = await resp.json()
            assert body["needs_install"] is True and body["target"]["key"] == "proj"
            resp = await client.post("/api/vscode/open", json={"workspace": "ghost"}, headers=BEARER)
            assert resp.status == 404
            resp = await client.get("/vscode/proj/", headers=BEARER)
            assert resp.status == 503
        finally:
            await client.close()
    asyncio.run(run())


def test_status_lists_targets_and_settings_roundtrip(ws_tree):
    async def run():
        client = await _client(_app())
        try:
            data = await (await client.get("/api/vscode", headers=BEARER)).json()
            assert data["supported"] is True
            assert [t["key"] for t in data["targets"]] == ["proj", "proj~wt-a", "proj~wt-b"]
            assert data["targets"][1]["path"] == "vscode/proj~wt-a/"
            assert data["install"]["version"] == vs.VERSION
            resp = await client.put("/api/vscode/settings", json={"idle_timeout": 30}, headers=BEARER)
            assert resp.status == 400
            resp = await client.put(
                "/api/vscode/settings", json={"idle_timeout": 120, "port_proxy": True}, headers=BEARER)
            assert (await resp.json())["settings"]["port_proxy"] is True
        finally:
            await client.close()
    asyncio.run(run())


def test_proxy_end_to_end_with_a_stand_in_server(ws_tree, fake_cs):
    async def run():
        app = _app()
        client = await _client(app)
        try:
            resp = await client.post(
                "/api/vscode/open", json={"workspace": "proj", "worktree": "wt-a"}, headers=BEARER)
            assert resp.status == 200, await resp.text()
            opened = await resp.json()
            assert opened["path"] == "vscode/proj~wt-a/"
            assert opened["server"]["state"] == "running"

            argv = json.loads((vs.server_dir("proj~wt-a") / "argv.json").read_text())
            assert argv[argv.index("--auth") + 1] == "none"
            assert "--disable-proxy" in argv
            assert argv[-1] == str(ws_tree / ".claude" / "worktrees" / "wt-a")
            assert argv[argv.index("--extensions-dir") + 1] == str(vs.shared_dir() / "extensions")
            assert argv[argv.index("--socket-mode") + 1] == "600"
            assert Path(argv[argv.index("--socket") + 1]) == vs.socket_path("proj~wt-a")
            # Shared User settings: the per-server file is a link to one copy.
            user_settings = vs.server_dir("proj~wt-a") / "User" / "settings.json"
            assert user_settings.is_symlink()
            assert user_settings.resolve() == (vs.shared_dir() / "User" / "settings.json").resolve()

            # No trailing slash: a relative redirect, so it works under /t/<name>/ too.
            resp = await client.get("/vscode/proj~wt-a", headers=BEARER, allow_redirects=False)
            assert resp.status == 302 and resp.headers["Location"] == "proj~wt-a/"
            resp = await client.get("/vscode/proj~wt-a/", headers=BEARER, allow_redirects=False)
            assert resp.status == 302 and resp.headers["Location"].startswith("./?folder=")

            # What reaches the server: no daemon credential, other cookies intact.
            resp = await client.post(
                "/vscode/proj~wt-a/echo/x?q=1",
                headers={**BEARER, "Cookie": "claunch_session=abc; relay_session=r; editor=1"},
                data="hello",
            )
            seen = await resp.json()
            assert seen["path"] == "/echo/x?q=1" and seen["body"] == "hello"
            lowered = {k.lower(): v for k, v in seen["headers"].items()}
            assert "authorization" not in lowered
            assert lowered["cookie"] == "editor=1"

            # Port forwarding is off: refused here, whatever the server would do.
            for path in ("proxy/3000/", "absproxy/3000/"):
                resp = await client.get(f"/vscode/proj~wt-a/{path}", headers=BEARER)
                assert resp.status == 403, path

            # WebSockets pass through, and count as a connection while open.
            server = app["vscode"].servers["proj~wt-a"]
            ws = await client.ws_connect("/vscode/proj~wt-a/sock", headers=BEARER)
            await ws.send_str("hi")
            msg = await ws.receive(timeout=5)
            assert msg.type == WSMsgType.TEXT and msg.data == "echo:hi"
            await ws.send_bytes(b"abc")
            msg = await ws.receive(timeout=5)
            assert msg.data == b"cba"
            assert server.connections == 1
            await ws.close()
            for _ in range(50):
                if server.connections == 0:
                    break
                await asyncio.sleep(0.05)
            assert server.connections == 0

            resp = await client.post("/api/vscode/stop", json={"key": "proj~wt-a"}, headers=BEARER)
            assert resp.status == 200
            assert server.state == "stopped" and server.proc.returncode is not None
            # A stopped server comes back on the next request.
            resp = await client.post("/vscode/proj~wt-a/echo/again", headers=BEARER, data="")
            assert resp.status == 200
            assert app["vscode"].servers["proj~wt-a"].state == "running"
        finally:
            await client.close()
    asyncio.run(run())


def test_port_proxy_setting_reaches_the_server(ws_tree, fake_cs):
    vs.update_settings({"port_proxy": True})

    async def run():
        client = await _client(_app())
        try:
            resp = await client.post("/api/vscode/open", json={"workspace": "proj"}, headers=BEARER)
            assert resp.status == 200, await resp.text()
            argv = json.loads((vs.server_dir("proj") / "argv.json").read_text())
            assert "--disable-proxy" not in argv
            resp = await client.get("/vscode/proj/proxy/3000/", headers=BEARER)
            assert resp.status == 200
        finally:
            await client.close()
    asyncio.run(run())


def test_session_outside_the_registry_gets_an_adhoc_server(tmp_path, fake_cs):
    loose = tmp_path / "loose"
    loose.mkdir()

    async def run():
        app = _app()
        sess = type("S", (), {"sdef": type("D", (), {"cwd": str(loose)})()})()
        app["manager"].get = lambda name: sess if name == "s1" else (_ for _ in ()).throw(KeyError(name))
        client = await _client(app)
        try:
            resp = await client.post("/api/vscode/open", json={"session": "s1"}, headers=BEARER)
            assert resp.status == 200, await resp.text()
            opened = await resp.json()
            assert opened["server"]["kind"] == "adhoc"
            assert opened["server"]["folder"] == str(loose.resolve())
            resp = await client.get(f"/{opened['path']}echo/x", headers=BEARER)
            assert (await resp.json())["folder"] == str(loose.resolve())
            # A typed path never makes one: only workspace names or a session.
            resp = await client.post("/api/vscode/open", json={"path": str(loose)}, headers=BEARER)
            assert resp.status == 400
        finally:
            await client.close()
    asyncio.run(run())


def test_idle_servers_are_stopped_by_the_sweep(ws_tree, fake_cs):
    async def run():
        app = _app()
        client = await _client(app)
        try:
            await client.post("/api/vscode/open", json={"workspace": "proj"}, headers=BEARER)
            mgr = app["vscode"]
            server = mgr.servers["proj"]
            await mgr.sweep()
            assert server.state == "running"
            server.last_active -= vs.DEFAULTS["idle_timeout"] + 1
            server.connections = 1
            await mgr.sweep()
            assert server.state == "running", "an open connection keeps it"
            server.connections = 0
            await mgr.sweep()
            assert server.state == "stopped"
        finally:
            await client.close()
    asyncio.run(run())


def test_turned_off_and_windows_refuse(ws_tree, monkeypatch):
    async def run():
        client = await _client(_app())
        try:
            vs.update_settings({"enabled": False})
            resp = await client.get("/vscode/proj/", headers=BEARER)
            assert resp.status == 404
            resp = await client.post("/api/vscode/open", json={"workspace": "proj"}, headers=BEARER)
            assert resp.status == 400
            vs.update_settings({"enabled": True})
            monkeypatch.setattr(vs.sys, "platform", "win32")
            data = await (await client.get("/api/vscode", headers=BEARER)).json()
            assert data["supported"] is False and data["reason"]
            resp = await client.post("/api/vscode/open", json={"workspace": "proj"}, headers=BEARER)
            assert resp.status == 400
            resp = await client.post("/api/vscode/install", headers=BEARER)
            assert resp.status == 400
        finally:
            await client.close()
    asyncio.run(run())


# --------------------------------------------------------------------------- #
# through a relay
# --------------------------------------------------------------------------- #
RELAY_EXE = find_relay_exe()


def _free_port() -> int:
    import socket

    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


@pytest.mark.skipif(RELAY_EXE is None, reason="no psmux-relay binary built")
def test_proxy_through_a_relay(ws_tree, fake_cs, tmp_path):
    """The relay strips /t/<name>, keeps Host and forwards the upgrade; the
    daemon's gate and the proxy's header rules hold on that path too."""
    import aiohttp

    from claude_launcher.daemon.relay_uplink import RelayUplink

    password, backend_token, name = "pw-vscode", "bt-vscode", "vspc"

    async def run():
        relay_port = _free_port()
        cfg = tmp_path / "relay"
        cfg.mkdir()
        relay = subprocess.Popen(
            [str(RELAY_EXE), "--ws-plain", "--ws-addr", f"127.0.0.1:{relay_port}",
             "--config", str(cfg / "relay.toml"), "--password", password,
             "--backend-token", backend_token, "--web-dir", str(cfg / "noweb")],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
        )
        app = _app()
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        dport = _free_port()
        await web.TCPSite(runner, "127.0.0.1", dport).start()
        uplink = RelayUplink(url=f"ws://127.0.0.1:{relay_port}/", token=backend_token,
                             name=name, local_host="127.0.0.1", local_port=dport)
        uplink_task = asyncio.ensure_future(uplink.run())
        base = f"http://127.0.0.1:{relay_port}"
        try:
            async with aiohttp.ClientSession(cookie_jar=aiohttp.CookieJar(unsafe=True)) as http:
                deadline = time.monotonic() + 15
                while True:
                    try:
                        async with http.post(f"{base}/login", json={"password": password}) as r:
                            if r.status == 200:
                                break
                    except aiohttp.ClientError:
                        pass
                    assert time.monotonic() < deadline, "relay never came up"
                    await asyncio.sleep(0.2)
                while True:
                    async with http.get(f"{base}/t/{name}/api/health") as r:
                        if r.status == 200:
                            break
                    assert time.monotonic() < deadline, "backend never registered"
                    await asyncio.sleep(0.3)

                async with http.get(f"{base}/t/{name}/vscode/proj/", allow_redirects=False) as r:
                    assert r.status == 401, "reachable through the relay without the daemon login"
                async with http.post(f"{base}/t/{name}/api/auth/session", json={"token": "sekrit"}) as r:
                    assert r.status == 200
                async with http.post(f"{base}/t/{name}/api/vscode/open", json={"workspace": "proj"}) as r:
                    assert r.status == 200, await r.text()
                    path = (await r.json())["path"]
                async with http.get(f"{base}/t/{name}/vscode/proj", allow_redirects=False) as r:
                    assert r.status == 302 and r.headers["Location"] == "proj/"
                async with http.get(f"{base}/t/{name}/{path}echo/x") as r:
                    seen = await r.json()
                lowered = {k.lower(): v for k, v in seen["headers"].items()}
                assert seen["path"] == "/echo/x"
                assert "claunch_session" not in lowered.get("cookie", "")
                assert "relay_session" not in lowered.get("cookie", "")
                assert lowered["host"] == f"127.0.0.1:{relay_port}"
                async with http.get(f"{base}/t/{name}/{path}proxy/3000/") as r:
                    assert r.status == 403
                ws = await http.ws_connect(f"{base}/t/{name}/{path}sock")
                await ws.send_str("relay")
                msg = await ws.receive(timeout=10)
                assert msg.type == WSMsgType.TEXT and msg.data == "echo:relay"
                await ws.close()
        finally:
            uplink.stop()
            uplink_task.cancel()
            with contextlib.suppress(BaseException):
                await uplink_task
            await runner.cleanup()
            relay.terminate()
            try:
                relay.wait(timeout=5)
            except subprocess.TimeoutExpired:
                relay.kill()

    asyncio.run(run())

