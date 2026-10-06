"""The daemon side of VS Code in the browser: servers, the proxy, the API.

:mod:`claude_launcher.vscode` decides *what* a key opens; this module runs
it. One code-server process per target, started when someone first opens it
and stopped after ``vscode.idle_timeout`` seconds with no open connection.
Each listens on a unix socket (mode 0600) with ``--auth none``: the only gate
is the daemon's own, because every request reaches it through
``/vscode/<key>/`` here, which the auth middleware covers exactly like
``/api/``. Through a relay the same path is ``/t/<name>/vscode/<key>/`` --
code-server builds its URLs and redirects relative to the page, so the prefix
needs nothing from this side.

What the proxy does to a request on the way in:

- drops ``Authorization`` and the daemon's and the relay's login cookies.
  code-server runs on the dashboard's origin (a relay cannot give it another
  one), so the least it can be handed is nothing that authenticates;
- refuses ``proxy/`` and ``absproxy/`` unless that server was started with
  port forwarding on (``vscode.port_proxy``). The server is also started with
  ``--disable-proxy`` then; this is the second lock on the same door.

POSIX only, like the rest of the feature.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import signal
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

import aiohttp
from aiohttp import WSMsgType, web

from .. import vscode as vs

log = logging.getLogger("claunch.daemon.vscode")

#: How long a starting server may take to answer before it counts as failed.
START_TIMEOUT = 60.0
#: How often the idle sweep looks at the servers.
SWEEP_INTERVAL = 15.0
#: How long a stopping server gets after SIGTERM before SIGKILL.
STOP_GRACE = 5.0

#: Cookies the proxy never forwards: the daemon's login and the relay's.
_PRIVATE_COOKIES = ("claunch_session", "relay_session")

#: Hop-by-hop headers (RFC 9110 7.6.1) plus the ones this proxy owns.
_HOP = frozenset({
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailer", "trailers", "transfer-encoding", "upgrade",
})
_WS_OWN = frozenset({
    "sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
    "sec-websocket-protocol", "sec-websocket-accept",
})


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


@dataclass
class Server:
    target: vs.Target
    socket: Path
    port_proxy: bool
    state: str = "starting"  # starting | running | stopped | failed
    proc: Optional[asyncio.subprocess.Process] = None
    started_at: str = ""
    last_active: float = field(default_factory=time.monotonic)
    last_active_wall: str = field(default_factory=_now_iso)
    connections: int = 0
    error: str = ""
    http: Optional[aiohttp.ClientSession] = None

    def touch(self) -> None:
        self.last_active = time.monotonic()
        self.last_active_wall = _now_iso()

    def to_dict(self) -> dict:
        return {
            **self.target.to_dict(),
            "state": self.state,
            "pid": self.proc.pid if self.proc is not None else None,
            "started_at": self.started_at,
            "last_active": self.last_active_wall,
            "connections": self.connections,
            "port_proxy": self.port_proxy,
            "error": self.error,
            "path": proxy_path(self.target.key),
        }


def proxy_path(key: str) -> str:
    """Where a target is served, relative to the dashboard's base."""
    return f"vscode/{key}/"


class InstallState:
    """The one install a daemon may have running, and how far it got."""

    def __init__(self) -> None:
        self.state = "idle"  # idle | installing | failed
        self.done = 0
        self.total = 0
        self.error = ""
        self.task: Optional[asyncio.Task] = None

    def to_dict(self) -> dict:
        binary = vs.binary()
        return {
            "version": vs.VERSION,
            "installed": binary is not None,
            "binary": binary or "",
            "state": "installed" if binary and self.state != "installing" else self.state,
            "done": self.done,
            "total": self.total,
            "error": self.error,
        }


class VSCodeManager:
    """Every code-server this daemon runs, keyed by target key."""

    def __init__(self, install_fetch=None) -> None:
        self.servers: Dict[str, Server] = {}
        #: Ad-hoc targets this daemon has opened for a session. Their keys are
        #: digests, so only the daemon that made one can say what it opens.
        self.adhoc: Dict[str, vs.Target] = {}
        self.install_state = InstallState()
        self._locks: Dict[str, asyncio.Lock] = {}
        self._install_fetch = install_fetch

    # ---- resolving ------------------------------------------------------
    def resolve(self, key: str) -> Optional[vs.Target]:
        server = self.servers.get(key)
        if server is not None:
            return server.target
        if key in self.adhoc:
            return self.adhoc[key]
        return vs.key_target(key)

    # ---- installing -----------------------------------------------------
    def start_install(self) -> dict:
        st = self.install_state
        if st.state == "installing":
            return st.to_dict()
        if vs.binary() is not None:
            return st.to_dict()
        st.state, st.done, st.total, st.error = "installing", 0, 0, ""

        def progress(done: int, total: int) -> None:
            st.done, st.total = done, total

        async def run() -> None:
            try:
                await asyncio.to_thread(vs.install, progress, self._install_fetch)
                st.state = "idle"
            except Exception as exc:  # noqa: BLE001 -- reported to the page
                log.warning("code-server install failed: %s", exc)
                st.state, st.error = "failed", str(exc)

        st.task = asyncio.create_task(run())
        return st.to_dict()

    # ---- running --------------------------------------------------------
    async def ensure(self, target: vs.Target) -> Server:
        """A running server for ``target``, starting one if needed."""
        lock = self._locks.setdefault(target.key, asyncio.Lock())
        async with lock:
            server = self.servers.get(target.key)
            if server is not None and server.state == "running" and _alive(server):
                return server
            if server is not None:
                await self._halt(server)
            return await self._start(target)

    async def _start(self, target: vs.Target) -> Server:
        reason = vs.unsupported_reason()
        if reason:
            raise vs.VSCodeError(reason)
        cfg = vs.settings()
        binary = vs.binary(cfg)
        if binary is None:
            raise vs.VSCodeError("code-server is not installed")
        if not Path(target.folder).is_dir():
            raise vs.VSCodeError(f"{target.folder} is not a directory")
        sock = vs.socket_path(target.key)
        sock.parent.mkdir(parents=True, exist_ok=True)
        with contextlib.suppress(OSError):
            os.chmod(sock.parent, 0o700)
        data = vs.server_dir(target.key)
        data.mkdir(parents=True, exist_ok=True)
        _kill_leftover(data, sock)
        with contextlib.suppress(FileNotFoundError):
            sock.unlink()
        _link_shared_settings(data)
        empty_config = vs.root_dir() / "config.yaml"
        if not empty_config.exists():
            # code-server reads ~/.config/code-server/config.yaml unless told
            # otherwise, and a password set there would sit behind our auth.
            empty_config.write_text("", encoding="utf-8")
        port_proxy = bool(cfg.get("port_proxy"))
        argv = [
            binary,
            "--config", str(empty_config),
            "--auth", "none",
            "--socket", str(sock),
            "--socket-mode", "600",
            "--user-data-dir", str(data),
            "--extensions-dir", str(vs.shared_dir() / "extensions"),
            "--disable-telemetry",
            "--disable-update-check",
        ]
        if not port_proxy:
            argv.append("--disable-proxy")
        argv.append(target.folder)
        # Nothing that marks a managed session follows the editor's terminals.
        env = {k: v for k, v in os.environ.items() if not k.startswith("CLAUNCH_")}
        server = Server(target=target, socket=sock, port_proxy=port_proxy)
        self.servers[target.key] = server
        logfile = (data / "server.log").open("ab")
        try:
            server.proc = await asyncio.create_subprocess_exec(
                *argv, cwd=target.folder, env=env,
                stdin=asyncio.subprocess.DEVNULL, stdout=logfile,
                stderr=asyncio.subprocess.STDOUT, start_new_session=True,
            )
        except OSError as exc:
            server.state, server.error = "failed", f"could not start code-server: {exc}"
            raise vs.VSCodeError(server.error) from exc
        finally:
            logfile.close()
        (data / "pid").write_text(str(server.proc.pid), encoding="utf-8")
        server.started_at = _now_iso()
        server.http = aiohttp.ClientSession(
            connector=aiohttp.UnixConnector(path=str(sock)),
            auto_decompress=False,
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
        )
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            if server.proc.returncode is not None:
                break
            if sock.exists():
                with contextlib.suppress(aiohttp.ClientError, OSError, asyncio.TimeoutError):
                    async with server.http.get("http://localhost/healthz") as resp:
                        if resp.status == 200:
                            server.state = "running"
                            server.touch()
                            log.info("code-server for %s up (pid %s)", target.key, server.proc.pid)
                            return server
            await asyncio.sleep(0.25)
        tail = _log_tail(data / "server.log")
        server.error = "code-server did not come up" + (f": {tail}" if tail else "")
        await self._halt(server)
        server.state = "failed"
        raise vs.VSCodeError(server.error)

    async def stop(self, key: str) -> bool:
        server = self.servers.get(key)
        if server is None:
            return False
        async with self._locks.setdefault(key, asyncio.Lock()):
            await self._halt(server)
        return True

    async def _halt(self, server: Server) -> None:
        proc = server.proc
        if proc is not None and proc.returncode is None:
            _signal_group(proc.pid, signal.SIGTERM)
            try:
                await asyncio.wait_for(proc.wait(), STOP_GRACE)
            except asyncio.TimeoutError:
                _signal_group(proc.pid, signal.SIGKILL)
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(proc.wait(), STOP_GRACE)
        if server.http is not None:
            await server.http.close()
            server.http = None
        with contextlib.suppress(FileNotFoundError):
            server.socket.unlink()
        if server.state != "failed":
            server.state = "stopped"
        server.connections = 0

    async def stop_all(self) -> None:
        for key in list(self.servers):
            await self.stop(key)

    async def sweep(self) -> None:
        """Stop idle servers; notice the ones that died on their own."""
        idle = float(vs.settings().get("idle_timeout") or vs.DEFAULTS["idle_timeout"])
        now = time.monotonic()
        for key, server in list(self.servers.items()):
            if server.state != "running":
                continue
            if not _alive(server):
                tail = _log_tail(vs.server_dir(key) / "server.log")
                await self._halt(server)
                server.state = "failed"
                server.error = "code-server exited" + (f": {tail}" if tail else "")
                continue
            if server.connections == 0 and now - server.last_active > idle:
                log.info("stopping idle code-server for %s", key)
                await self.stop(key)

    async def watch(self) -> None:
        while True:
            await asyncio.sleep(SWEEP_INTERVAL)
            try:
                await self.sweep()
            except Exception:  # noqa: BLE001 -- the sweep must keep running
                log.exception("vscode sweep failed")


def _alive(server: Server) -> bool:
    return server.proc is not None and server.proc.returncode is None


def _signal_group(pid: int, sig: int) -> None:
    with contextlib.suppress(ProcessLookupError, PermissionError):
        os.killpg(pid, sig)


def _kill_leftover(data: Path, sock: Path) -> None:
    """End a server a previous daemon left running on this socket.

    A daemon that crashed does not stop its children; the one after it finds
    their socket in the way. Only a process whose command line names this
    exact socket is touched -- a recycled pid is somebody else's.
    """
    pidfile = data / "pid"
    try:
        pid = int(pidfile.read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return
    try:
        cmdline = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return
    if str(sock).encode() in cmdline:
        _signal_group(pid, signal.SIGKILL)
    with contextlib.suppress(OSError):
        pidfile.unlink()


def _link_shared_settings(data: Path) -> None:
    """Point this server's User settings and keybindings at the shared copy.

    Extensions are shared through ``--extensions-dir``; settings have no such
    flag, so the per-server files are symlinks to one file each. A real file
    already there (the user edited before the link existed) is left alone.
    """
    shared_user = vs.shared_dir() / "User"
    shared_user.mkdir(parents=True, exist_ok=True)
    (vs.shared_dir() / "extensions").mkdir(parents=True, exist_ok=True)
    user = data / "User"
    user.mkdir(parents=True, exist_ok=True)
    for name in ("settings.json", "keybindings.json"):
        shared = shared_user / name
        if not shared.exists():
            shared.write_text("{}\n" if name == "settings.json" else "[]\n", encoding="utf-8")
        mine = user / name
        if mine.is_symlink() or mine.exists():
            continue
        with contextlib.suppress(OSError):
            mine.symlink_to(shared)


def _log_tail(path: Path, lines: int = 3) -> str:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return " | ".join(line.strip() for line in text.strip().splitlines()[-lines:])


# --------------------------------------------------------------------------- #
# the proxy
# --------------------------------------------------------------------------- #
def _forward_headers(request: web.Request, *, websocket: bool) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for name, value in request.headers.items():
        low = name.lower()
        if low in _HOP or low == "authorization" or low == "cookie":
            continue
        if websocket and low in _WS_OWN:
            continue
        out[name] = value
    cookies = [
        part.strip() for part in request.headers.get("Cookie", "").split(";")
        if part.strip() and part.split("=", 1)[0].strip() not in _PRIVATE_COOKIES
    ]
    if cookies:
        out["Cookie"] = "; ".join(cookies)
    return out


def _page(status: int, text: str) -> web.Response:
    body = (
        "<!doctype html><meta charset=utf-8><title>VS Code</title>"
        f"<p style='font:15px system-ui;margin:2em'>{text}</p>"
    )
    return web.Response(status=status, text=body, content_type="text/html")


async def h_redirect(request: web.Request) -> web.Response:
    # Relative, so it lands under /t/<name>/ just as well as under /.
    key = request.match_info["key"]
    raise web.HTTPFound(f"{key}/" + (f"?{request.query_string}" if request.query_string else ""))


async def h_proxy(request: web.Request) -> web.StreamResponse:
    mgr: VSCodeManager = request.app["vscode"]
    key = request.match_info["key"]
    tail = request.match_info.get("tail", "")
    cfg = vs.settings()
    if not cfg.get("enabled"):
        return _page(404, "VS Code servers are turned off in this daemon's settings.")
    reason = vs.unsupported_reason()
    if reason:
        return _page(404, reason)
    target = mgr.resolve(key)
    if target is None:
        return _page(404, "No workspace or worktree goes by this name. Open it from the dashboard.")
    server = mgr.servers.get(key)
    if server is None or server.state != "running" or not _alive(server):
        if vs.binary(cfg) is None:
            return _page(503, "code-server is not installed. Open this workspace from the dashboard to install it.")
        try:
            server = await mgr.ensure(target)
        except vs.VSCodeError as exc:
            return _page(502, f"Could not start VS Code: {exc}")
    first = tail.split("/", 1)[0]
    if first in ("proxy", "absproxy") and not server.port_proxy:
        return _page(403, "Port forwarding is turned off (vscode.port_proxy).")
    upstream = f"http://localhost/{tail}"
    if request.query_string:
        upstream += f"?{request.query_string}"
    if request.headers.get("Upgrade", "").lower() == "websocket":
        return await _proxy_websocket(request, server, upstream)
    return await _proxy_http(request, server, upstream)


async def _proxy_http(request: web.Request, server: Server, upstream: str) -> web.StreamResponse:
    headers = _forward_headers(request, websocket=False)
    data = request.content if request.body_exists else None
    server.touch()
    try:
        async with server.http.request(
            request.method, upstream, headers=headers, data=data,
            allow_redirects=False,
        ) as up:
            resp = web.StreamResponse(status=up.status, reason=up.reason)
            for name, value in up.headers.items():
                low = name.lower()
                if low in _HOP or low == "content-length":
                    continue
                resp.headers.add(name, value)
            if up.content_length is not None:
                resp.content_length = up.content_length
            await resp.prepare(request)
            async for chunk in up.content.iter_chunked(64 * 1024):
                await resp.write(chunk)
            await resp.write_eof()
            return resp
    except (aiohttp.ClientError, OSError) as exc:
        return _page(502, f"VS Code did not answer: {exc}")
    finally:
        server.touch()


async def _proxy_websocket(request: web.Request, server: Server, upstream: str) -> web.StreamResponse:
    headers = _forward_headers(request, websocket=True)
    protocols = [
        p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",") if p.strip()
    ]
    try:
        up = await server.http.ws_connect(
            upstream.replace("http://", "ws://", 1), headers=headers,
            protocols=protocols, max_msg_size=0, autoping=True,
        )
    except (aiohttp.ClientError, OSError) as exc:
        return _page(502, f"VS Code refused the connection: {exc}")
    down = web.WebSocketResponse(
        protocols=[up.protocol] if up.protocol else (), max_msg_size=0, autoping=True,
    )
    await down.prepare(request)
    server.connections += 1
    server.touch()

    async def pump(src, dst) -> None:
        async for msg in src:
            if msg.type == WSMsgType.TEXT:
                await dst.send_str(msg.data)
            elif msg.type == WSMsgType.BINARY:
                await dst.send_bytes(msg.data)
            elif msg.type in (WSMsgType.CLOSE, WSMsgType.CLOSING, WSMsgType.CLOSED, WSMsgType.ERROR):
                break

    tasks = [asyncio.ensure_future(pump(down, up)), asyncio.ensure_future(pump(up, down))]
    try:
        await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in tasks:
            task.cancel()
        with contextlib.suppress(Exception):
            await up.close()
        with contextlib.suppress(Exception):
            await down.close()
        server.connections = max(0, server.connections - 1)
        server.touch()
    return down


# --------------------------------------------------------------------------- #
# the API
# --------------------------------------------------------------------------- #
def _json(payload, status: int = 200) -> web.Response:
    return web.json_response(payload, status=status)


def _snapshot(mgr: VSCodeManager) -> dict:
    reason = vs.unsupported_reason()
    targets = []
    for target in vs.list_targets():
        server = mgr.servers.get(target.key)
        targets.append({
            **target.to_dict(),
            "path": proxy_path(target.key),
            "server": server.to_dict() if server is not None else None,
        })
    return {
        "supported": reason is None,
        "reason": reason or "",
        "settings": vs.settings(),
        "defaults": dict(vs.DEFAULTS),
        "install": mgr.install_state.to_dict(),
        "targets": targets,
        "servers": [s.to_dict() for s in mgr.servers.values()],
        "root": str(vs.root_dir()),
    }


async def h_status(request: web.Request) -> web.Response:
    return _json(_snapshot(request.app["vscode"]))


async def h_settings(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        return _json({"error": "expected a JSON object"}, 400)
    try:
        cfg = vs.update_settings(body if isinstance(body, dict) else None)
    except vs.VSCodeError as exc:
        return _json({"error": str(exc)}, 400)
    return _json({"settings": cfg})


async def h_install(request: web.Request) -> web.Response:
    reason = vs.unsupported_reason()
    if reason:
        return _json({"error": reason}, 400)
    return _json({"install": request.app["vscode"].start_install()}, 202)


async def h_open(request: web.Request) -> web.Response:
    """Start (or find) the server for a workspace, a worktree or a session.

    Body: ``{"workspace": name, "worktree"?: name}`` or ``{"session": name}``.
    A session's directory may be outside every workspace; that is the one
    way an ad-hoc server is made. Answers 409 ``needs_install`` when
    code-server is missing, so the page can ask before downloading.
    """
    mgr: VSCodeManager = request.app["vscode"]
    try:
        body = await request.json()
    except ValueError:
        body = None
    if not isinstance(body, dict):
        return _json({"error": "expected a JSON object"}, 400)
    if not vs.settings().get("enabled"):
        return _json({"error": "VS Code servers are turned off (vscode.enabled)"}, 400)
    reason = vs.unsupported_reason()
    if reason:
        return _json({"error": reason}, 400)
    try:
        if body.get("session"):
            manager = request.app["manager"]
            try:
                session = manager.get(str(body["session"]))
            except Exception as exc:  # noqa: BLE001 -- ManagerError and kin
                return _json({"error": str(exc)}, 404)
            cwd = getattr(session.sdef, "cwd", "") or ""
            if not cwd:
                return _json({"error": "that session has no directory"}, 400)
            target = vs.path_target(cwd)
            if target.kind == "adhoc":
                mgr.adhoc[target.key] = target
        elif body.get("workspace"):
            target = vs.workspace_target(str(body["workspace"]), body.get("worktree") or None)
        else:
            return _json({"error": "name a workspace or a session"}, 400)
    except vs.VSCodeError as exc:
        return _json({"error": str(exc)}, 404)
    if vs.binary() is None:
        return _json({
            "needs_install": True, "target": target.to_dict(),
            "install": mgr.install_state.to_dict(),
        }, 409)
    try:
        server = await mgr.ensure(target)
    except vs.VSCodeError as exc:
        return _json({"error": str(exc), "target": target.to_dict()}, 502)
    return _json({"server": server.to_dict(), "path": proxy_path(target.key)})


async def h_stop(request: web.Request) -> web.Response:
    try:
        body = await request.json()
    except ValueError:
        body = None
    key = str((body or {}).get("key") or "") if isinstance(body, dict) else ""
    if not key:
        return _json({"error": "name the server's key"}, 400)
    stopped = await request.app["vscode"].stop(key)
    if not stopped:
        return _json({"error": f"no server for {key!r}"}, 404)
    return _json({"stopped": key})


async def _lifecycle(app):
    mgr: VSCodeManager = app["vscode"]
    task = asyncio.create_task(mgr.watch())
    try:
        yield
    finally:
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        await mgr.stop_all()


def install(app: web.Application, manager: Optional[VSCodeManager] = None) -> None:
    app["vscode"] = manager if manager is not None else VSCodeManager()
    app.cleanup_ctx.append(_lifecycle)
    # At shutdown, not only at cleanup: an open editor tab holds a proxied
    # socket, and stopping the servers is what ends it.
    app.on_shutdown.append(lambda app: app["vscode"].stop_all())
    r = app.router
    r.add_get("/api/vscode", h_status)
    r.add_put("/api/vscode/settings", h_settings)
    r.add_post("/api/vscode/install", h_install)
    r.add_post("/api/vscode/open", h_open)
    r.add_post("/api/vscode/stop", h_stop)
    r.add_route("*", "/vscode/{key}", h_redirect)
    r.add_route("*", "/vscode/{key}/{tail:.*}", h_proxy)
