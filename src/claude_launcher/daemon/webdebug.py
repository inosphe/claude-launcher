"""The web debug page (claunch-dsb28): a page under development, framed by
the dashboard so the user and a session can both see and act on it.

Three parts, all in this module:

* **The proxy.** The page under test is never framed directly. A dev server
  at ``http://localhost:5173`` is another origin than the dashboard, and a
  parent page cannot read or drive a cross-origin frame. So the daemon opens
  one listener per target origin (``Proxy``), on a port of its own, and the
  frame loads the target through it. Every HTML response gets one script tag,
  ``/__claunch/probe.js`` (web/static/webdebug-probe.js), and the probe
  answers the parent page over ``postMessage``: outline the DOM, pick an
  element, click, type, read the console.

  A port of its own rather than a path under the daemon (as /vscode/ is):
  * root-absolute URLs (``/src/main.ts``, ``/@vite/client``, ES module
    imports) resolve against the proxy's root, which *is* the target's root,
    so nothing in the page needs rewriting;
  * the proxied page runs at an origin that is not the dashboard's, so its
    scripts cannot read the daemon's API with the user's login. A same-origin
    proxy would hand every script on the page under test -- its CDN and
    analytics tags included -- the daemon's authority.

  That origin is still the dashboard's *site* (same host, another port), and
  the login cookie's SameSite=Strict does not tell ports apart: a POST or a
  WebSocket the proxied page opens to the daemon carries the cookie, and CORS
  hides only the response. What refuses it is the daemon's auth middleware,
  which turns away a cookie-authenticated request whose Sec-Fetch-Site (or,
  without that, Origin) names another page (api._foreign_origin).

  The listener is not open to whoever reaches the port: a request needs the
  proxy's cookie, which only ``/__claunch/enter?k=<secret>`` sets, and the
  secret is only handed out by an authenticated ``/api/webdebug/proxy``
  call. The daemon's own login cookie (host-scoped, so the browser sends it
  to every port) and the proxy cookies are stripped before a request goes
  upstream.

* **Windows.** Each open web debug page holds one WebSocket
  (``/api/webdebug/ws``) and says which session it is showing. The user
  picks that session in the page's top bar; only that session's agent can
  drive the window.

* **Commands and sends.** A session's MCP tools (webdebug_mcp.py) post a
  command to ``/api/webdebug/command``; the daemon forwards it to the window
  and waits for the page's answer. The other direction is always the user's
  explicit act: a picked element or a screenshot goes to the session only
  when the user presses Send (``/api/webdebug/send``), typed into the
  session as a ``[claunch webdebug w-...]`` block.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import logging
import re
import secrets
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import aiohttp
from aiohttp import WSMsgType, web

from . import paths

log = logging.getLogger(__name__)

PROBE_PATH = "/__claunch/probe.js"
ENTER_PATH = "/__claunch/enter"
PROBE_FILE = Path(__file__).resolve().parent.parent / "web" / "static" / "webdebug-probe.js"
PAGE_FILE = Path(__file__).resolve().parent.parent / "web" / "static" / "webdebug.html"

#: Cookies the browser sends to every port of this host that must never
#: reach the page under test: the daemon's login (api.COOKIE_PREFIX) and the
#: proxies' own.
_PRIVATE_COOKIE_PREFIXES = ("claunch_session", "claunch_wd_")

#: Request headers not forwarded upstream. ``accept-encoding`` too: the
#: client decompresses and the response is passed on decoded (the probe is
#: injected into the decoded HTML).
_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host",
    "accept-encoding", "content-length",
}
_WS_OWN = {"sec-websocket-key", "sec-websocket-version", "sec-websocket-extensions",
           "sec-websocket-protocol"}
#: Response headers dropped: framing refusals (the page must load in the
#: frame, and the probe is a script the page's CSP does not list) and the
#: encoding the client already undid.
_DROP_RESPONSE = {
    "x-frame-options", "content-security-policy", "content-security-policy-report-only",
    "content-encoding", "content-length", "transfer-encoding", "connection", "keep-alive",
}

#: What the MCP tools may ask a window to do. The page and the probe
#: implement them; anything else is refused here before it reaches a page.
OPS = frozenset({
    "snapshot", "query", "click", "type", "press", "scroll", "navigate",
    "eval", "console", "screenshot", "highlight",
})

COMMAND_TIMEOUT = 20.0
MAX_COMMAND_TIMEOUT = 120.0
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_IMAGE_ID = re.compile(r"^[0-9a-f]{20}\.(png|jpg|webp)$")
_WINDOW_ID = re.compile(r"^w-[0-9a-z]{4,16}$")
_ORIGIN = re.compile(r"^https?://[^/\s]+$")

PREFIX = "[claunch webdebug"


class WebDebugError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


# --------------------------------------------------------------------------- #
# URLs and HTML
# --------------------------------------------------------------------------- #
def split_target(raw: str) -> Tuple[str, str]:
    """``(origin, path)`` of a URL the user typed in the address bar.

    A bare ``localhost:3000/x`` is read as http. Only http(s) is proxied.
    """
    text = str(raw or "").strip()
    if not text:
        raise WebDebugError(400, "an address is required")
    if not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*://", text):
        text = "http://" + text
    parts = urllib.parse.urlsplit(text)
    scheme = parts.scheme.lower()
    if scheme not in ("http", "https"):
        raise WebDebugError(400, f"only http and https pages can be opened, not {scheme}:")
    if not parts.hostname:
        raise WebDebugError(400, f"{raw!r} names no host")
    host = parts.hostname.lower()
    if ":" in host:
        host = f"[{host}]"
    try:
        port = parts.port
    except ValueError:
        raise WebDebugError(400, f"{raw!r} has an invalid port") from None
    origin = f"{scheme}://{host}" + (f":{port}" if port else "")
    path = parts.path or "/"
    if parts.query:
        path += "?" + parts.query
    if parts.fragment:
        path += "#" + parts.fragment
    return origin, path


_HEAD = re.compile(rb"<head(\s[^>]*)?>", re.IGNORECASE)
_HTML = re.compile(rb"<html(\s[^>]*)?>", re.IGNORECASE)
_DOCTYPE = re.compile(rb"<!doctype[^>]*>", re.IGNORECASE)
PROBE_TAG = b'<script src="' + PROBE_PATH.encode() + b'"></script>'


def inject_probe(body: bytes) -> bytes:
    """``body`` with the probe's script tag as early as it can go: first in
    ``<head>``, so it sees the page's own console output and errors from the
    start. Never before the doctype (that would put the page in quirks
    mode)."""
    for pattern in (_HEAD, _HTML, _DOCTYPE):
        m = pattern.search(body)
        if m:
            return body[:m.end()] + PROBE_TAG + body[m.end():]
    return PROBE_TAG + body


def _private_cookie(name: str) -> bool:
    return name.startswith(_PRIVATE_COOKIE_PREFIXES)


def _clean_set_cookie(value: str) -> str:
    """A target's Set-Cookie made to stick on the proxy's origin: no
    ``Domain`` (the proxy is another host name than the target may be) and no
    ``Secure``/``SameSite=None`` (the proxy speaks plain http)."""
    parts = [p for p in value.split(";")]
    keep = [parts[0]]
    for p in parts[1:]:
        low = p.strip().lower()
        if low.startswith("domain=") or low == "secure" or low == "samesite=none":
            continue
        keep.append(p)
    return ";".join(keep)


def _page(status: int, text: str) -> web.Response:
    body = ("<!doctype html><meta charset=utf-8><title>claunch webdebug</title>"
            f"<p style='font:14px system-ui;margin:2em'>{text}</p>")
    return web.Response(status=status, text=body, content_type="text/html")


# --------------------------------------------------------------------------- #
# the proxy
# --------------------------------------------------------------------------- #
class Proxy:
    """One listener in front of one target origin."""

    def __init__(self, origin: str, bind: str) -> None:
        self.origin = origin
        self.bind = bind
        self.secret = secrets.token_urlsafe(24)
        self.cookie = "claunch_wd_" + hashlib.sha256(self.secret.encode()).hexdigest()[:10]
        self.parents: set = set()
        self.port: Optional[int] = None
        self.runner: Optional[web.AppRunner] = None
        self.http: Optional[aiohttp.ClientSession] = None
        self.used_at = time.time()

    async def start(self) -> None:
        app = web.Application(client_max_size=64 * 1024 * 1024)
        app.router.add_route("*", "/{tail:.*}", self.handle)
        self.runner = web.AppRunner(app, access_log=None, shutdown_timeout=1.0)
        await self.runner.setup()
        site = web.TCPSite(self.runner, self.bind, 0)
        await site.start()
        self.port = self.runner.addresses[0][1]
        self.http = aiohttp.ClientSession(
            timeout=aiohttp.ClientTimeout(total=None, sock_connect=10),
            cookie_jar=aiohttp.DummyCookieJar(),
        )
        log.info("webdebug: proxy for %s on %s:%s", self.origin, self.bind, self.port)

    async def stop(self) -> None:
        if self.http is not None:
            with contextlib.suppress(Exception):
                await self.http.close()
        if self.runner is not None:
            with contextlib.suppress(Exception):
                await self.runner.cleanup()

    def enter_path(self, path: str) -> str:
        return ENTER_PATH + "?" + urllib.parse.urlencode({"k": self.secret, "to": path})

    def _authorized(self, request: web.Request) -> bool:
        value = request.cookies.get(self.cookie, "")
        return bool(value) and secrets.compare_digest(value, self.secret)

    async def handle(self, request: web.Request) -> web.StreamResponse:
        self.used_at = time.time()
        if request.path == ENTER_PATH:
            return self._enter(request)
        if not self._authorized(request):
            return _page(403, "This page is served by the claunch web debug proxy. "
                              "Open it from the dashboard's web debug page.")
        if request.path == PROBE_PATH:
            return self._probe()
        if request.headers.get("Upgrade", "").lower() == "websocket":
            return await self._websocket(request)
        return await self._http(request)

    def _enter(self, request: web.Request) -> web.Response:
        key = request.query.get("k", "")
        if not key or not secrets.compare_digest(key, self.secret):
            return _page(403, "This web debug link is not valid (the daemon may have restarted). "
                              "Open the page again from the dashboard.")
        to = request.query.get("to", "/")
        if not to.startswith("/") or to.startswith("//"):
            to = "/"
        parent = request.query.get("parent", "")
        if _ORIGIN.match(parent):
            self.parents.add(parent)
        resp = web.Response(status=302, headers={"Location": to})
        resp.set_cookie(self.cookie, self.secret, httponly=True, samesite="Lax", path="/")
        resp.headers["Cache-Control"] = "no-store"
        return resp

    def _probe(self) -> web.Response:
        try:
            body = PROBE_FILE.read_text(encoding="utf-8")
        except OSError:
            body = "console.warn('claunch webdebug: probe missing');"
        config = json.dumps({"parents": sorted(self.parents), "target": self.origin})
        text = f"(function(CONFIG){{\n{body}\n}})({config});\n"
        return web.Response(text=text, content_type="application/javascript",
                            headers={"Cache-Control": "no-store"})

    def _proxy_origin(self, request: web.Request) -> str:
        return f"{request.scheme}://{request.host}"

    def _headers(self, request: web.Request, *, websocket: bool) -> Dict[str, str]:
        mine = self._proxy_origin(request)
        out: Dict[str, str] = {}
        for name, value in request.headers.items():
            low = name.lower()
            if low in _HOP or low == "cookie" or (websocket and low in _WS_OWN):
                continue
            if low == "origin" and value == mine:
                value = self.origin
            elif low == "referer" and value.startswith(mine):
                value = self.origin + value[len(mine):]
            out[name] = value
        cookies = [
            part.strip() for part in request.headers.get("Cookie", "").split(";")
            if part.strip() and not _private_cookie(part.split("=", 1)[0].strip())
        ]
        if cookies:
            out["Cookie"] = "; ".join(cookies)
        out["Host"] = urllib.parse.urlsplit(self.origin).netloc
        return out

    def _location(self, value: str) -> str:
        if value.startswith(self.origin + "/") or value == self.origin:
            return value[len(self.origin):] or "/"
        return value

    async def _http(self, request: web.Request) -> web.StreamResponse:
        assert self.http is not None
        upstream = self.origin + request.rel_url.raw_path_qs
        data = request.content if request.body_exists else None
        try:
            async with self.http.request(
                request.method, upstream, headers=self._headers(request, websocket=False),
                data=data, allow_redirects=False, ssl=False,
            ) as up:
                html = (up.headers.get("Content-Type", "").lower().startswith("text/html")
                        and request.method != "HEAD")
                headers = []
                for name, value in up.headers.items():
                    low = name.lower()
                    if low in _DROP_RESPONSE:
                        continue
                    if low == "location":
                        value = self._location(value)
                    elif low == "set-cookie":
                        value = _clean_set_cookie(value)
                    headers.append((name, value))
                if html:
                    body = inject_probe(await up.read())
                    resp = web.Response(status=up.status, reason=up.reason, body=body)
                    for name, value in headers:
                        resp.headers.add(name, value)
                    return resp
                resp = web.StreamResponse(status=up.status, reason=up.reason)
                for name, value in headers:
                    resp.headers.add(name, value)
                await resp.prepare(request)
                async for chunk in up.content.iter_chunked(64 * 1024):
                    await resp.write(chunk)
                await resp.write_eof()
                return resp
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            return _page(502, f"{self.origin} did not answer: {exc}")

    async def _websocket(self, request: web.Request) -> web.StreamResponse:
        assert self.http is not None
        upstream = self.origin.replace("http", "ws", 1) + request.rel_url.raw_path_qs
        protocols = [p.strip() for p in request.headers.get("Sec-WebSocket-Protocol", "").split(",")
                     if p.strip()]
        try:
            up = await self.http.ws_connect(
                upstream, headers=self._headers(request, websocket=True), protocols=protocols,
                max_msg_size=0, autoping=True, ssl=False,
            )
        except (aiohttp.ClientError, OSError) as exc:
            return _page(502, f"{self.origin} refused the socket: {exc}")
        down = web.WebSocketResponse(protocols=[up.protocol] if up.protocol else (),
                                     max_msg_size=0, autoping=True)
        await down.prepare(request)

        async def pump(src, dst) -> None:
            async for msg in src:
                if msg.type == WSMsgType.TEXT:
                    await dst.send_str(msg.data)
                elif msg.type == WSMsgType.BINARY:
                    await dst.send_bytes(msg.data)
                else:
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
        return down


# --------------------------------------------------------------------------- #
# windows and commands
# --------------------------------------------------------------------------- #
@dataclass
class Window:
    id: str
    ws: web.WebSocketResponse
    session: str = ""
    url: str = ""
    title: str = ""
    sharing: bool = False
    opened_at: float = field(default_factory=time.time)
    focused_at: float = field(default_factory=time.time)
    pending: Dict[str, asyncio.Future] = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {"window": self.id, "session": self.session, "url": self.url, "title": self.title,
                "screen_shared": self.sharing,
                "opened_at": _iso(self.opened_at), "focused_at": _iso(self.focused_at)}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


class Hub:
    """The proxies and windows of one daemon."""

    def __init__(self) -> None:
        self.proxies: Dict[Tuple[str, str], Proxy] = {}
        self.windows: Dict[str, Window] = {}
        self._lock = asyncio.Lock()

    async def proxy_for(self, origin: str, bind: str) -> Proxy:
        async with self._lock:
            proxy = self.proxies.get((origin, bind))
            if proxy is None:
                proxy = Proxy(origin, bind)
                await proxy.start()
                self.proxies[(origin, bind)] = proxy
            return proxy

    async def stop_all(self) -> None:
        proxies, self.proxies = list(self.proxies.values()), {}
        for proxy in proxies:
            await proxy.stop()
        for win in list(self.windows.values()):
            self._drop(win)

    def windows_of(self, session: str) -> List[Window]:
        return sorted((w for w in self.windows.values() if w.session == session),
                      key=lambda w: w.focused_at, reverse=True)

    def window_for(self, session: str, window: str = "") -> Window:
        if window:
            win = self.windows.get(window)
            if win is None:
                raise WebDebugError(404, f"no web debug window {window!r} is open")
            if win.session != session:
                raise WebDebugError(403, f"window {window} is showing "
                                         f"{win.session or 'no session'}, not {session}; only the "
                                         "session the user picked in its top bar can drive it")
            return win
        mine = self.windows_of(session)
        if not mine:
            raise WebDebugError(404, (
                f"no web debug window is showing session {session}. Ask the user to open one "
                "(the 'web' button in the session's terminal header) and pick this session in "
                "its top bar."))
        return mine[0]

    async def command(self, session: str, op: str, args: dict, *, window: str = "",
                      timeout: float = COMMAND_TIMEOUT) -> dict:
        if op not in OPS:
            raise WebDebugError(400, f"unknown operation {op!r}")
        win = self.window_for(session, window)
        cid = secrets.token_hex(6)
        fut = asyncio.get_running_loop().create_future()
        win.pending[cid] = fut
        try:
            await win.ws.send_json({"type": "command", "id": cid, "op": op, "args": args or {}})
            result = await asyncio.wait_for(fut, timeout=max(1.0, min(timeout, MAX_COMMAND_TIMEOUT)))
        except asyncio.TimeoutError:
            raise WebDebugError(504, f"window {win.id} did not answer {op} within {timeout:g}s") from None
        except (ConnectionResetError, RuntimeError) as exc:
            raise WebDebugError(410, f"window {win.id} went away: {exc}") from None
        finally:
            win.pending.pop(cid, None)
        if not isinstance(result, dict):
            raise WebDebugError(502, "the page sent a malformed answer")
        if not result.get("ok"):
            raise WebDebugError(422, str(result.get("error") or f"{op} failed in the page"))
        value = result.get("value")
        if isinstance(value, dict) and value.get("image"):
            value = dict(value)
            value["image"] = str(image_path(value["image"]))
        return {"window": win.id, "op": op, "url": win.url, "value": value}

    def _drop(self, win: Window) -> None:
        self.windows.pop(win.id, None)
        for fut in win.pending.values():
            if not fut.done():
                fut.set_exception(ConnectionResetError("the page closed"))
        win.pending.clear()

    def receive(self, win: Window, msg: dict) -> None:
        kind = msg.get("type")
        if kind in ("hello", "state"):
            if "session" in msg:
                win.session = str(msg.get("session") or "")
            for key in ("url", "title"):
                if key in msg:
                    setattr(win, key, str(msg.get(key) or "")[:2000])
            if "sharing" in msg:
                win.sharing = bool(msg.get("sharing"))
            if msg.get("focused") or kind == "hello":
                win.focused_at = time.time()
        elif kind == "result":
            fut = win.pending.get(str(msg.get("id") or ""))
            if fut is not None and not fut.done():
                fut.set_result(msg)


# --------------------------------------------------------------------------- #
# images and the block typed into a session
# --------------------------------------------------------------------------- #
def image_folder() -> Path:
    return paths.daemon_dir() / "webdebug-images"


def image_path(name: str) -> Path:
    if not _IMAGE_ID.match(str(name or "")):
        raise WebDebugError(400, f"{name!r} is not a web debug image")
    return image_folder() / name


def _clip(text, limit: int) -> str:
    s = str(text or "")
    return s if len(s) <= limit else s[:limit] + "…"


def compose_send(window: str, body: dict, image: Optional[Path]) -> str:
    """The block a user's Send types into the session."""
    kind = body.get("kind")
    what = {"element": "an element they picked", "screenshot": "a screenshot",
            }.get(kind, "a note")
    lines = [f"{PREFIX} {window}] The user sent you {what} from the web debug page."]
    page = _clip(body.get("url"), 500)
    if page:
        title = _clip(body.get("title"), 200)
        lines.append(f"  page: {page}" + (f"  ({title})" if title else ""))
    note = str(body.get("note") or "").strip()
    if note:
        lines.extend("  > " + ln for ln in _clip(note, 4000).splitlines())
    el = body.get("element")
    if isinstance(el, dict):
        lines.append(f"  element: {_clip(el.get('label'), 200)}")
        if el.get("selector"):
            lines.append(f"  selector: {_clip(el.get('selector'), 500)}")
        if el.get("text"):
            lines.append(f"  text: {json.dumps(_clip(el.get('text'), 300), ensure_ascii=False)}")
        rect = el.get("rect")
        if isinstance(rect, dict):
            lines.append("  rect: " + " ".join(f"{k}={rect.get(k)}" for k in ("x", "y", "width", "height")))
        if el.get("styles"):
            lines.append(f"  styles: {_clip(el.get('styles'), 600)}")
        if el.get("html"):
            lines.append("  html:")
            lines.extend("    " + ln for ln in _clip(el.get("html"), 2000).splitlines()[:40])
    region = body.get("region")
    if isinstance(region, dict):
        lines.append("  region (page px): " + " ".join(f"{k}={region.get(k)}" for k in ("x", "y", "width", "height")))
    if image is not None:
        lines.append(f"  image: {image} (open it to see what the user means)")
    lines.append(
        f"You can look at and act on this page with the webdebug_* tools (window=\"{window}\"): "
        "webdebug_snapshot to read it, webdebug_click / webdebug_type to act, webdebug_console "
        "for its log, webdebug_screenshot when the user shares the screen.")
    return "\n".join(lines)


# --------------------------------------------------------------------------- #
# routes
# --------------------------------------------------------------------------- #
def _hub(request: web.Request) -> Hub:
    return request.app["webdebug"]


def _err(exc: WebDebugError) -> web.Response:
    return web.json_response({"error": str(exc)}, status=exc.status)


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise WebDebugError(400, "the body must be JSON") from None
    if not isinstance(body, dict):
        raise WebDebugError(400, "the body must be a JSON object")
    return body


async def h_page(_request: web.Request) -> web.StreamResponse:
    if not PAGE_FILE.is_file():
        return _page(404, "web debug page assets are missing")
    return web.FileResponse(PAGE_FILE, headers={"Cache-Control": "no-cache"})


def _bind_of(request: web.Request) -> str:
    """The address the dashboard reached the daemon on: the proxy listens
    there too, so the same browser reaches it."""
    sock = request.transport.get_extra_info("sockname") if request.transport else None
    host = sock[0] if sock else "127.0.0.1"
    return host if host not in ("", "0.0.0.0", "::") else "127.0.0.1"


async def h_proxy(request: web.Request) -> web.Response:
    try:
        body = await _json_body(request)
        origin, path = split_target(body.get("url"))
        proxy = await _hub(request).proxy_for(origin, _bind_of(request))
    except WebDebugError as exc:
        return _err(exc)
    except OSError as exc:
        return web.json_response({"error": f"could not open a proxy port: {exc}"}, status=500)
    return web.json_response({"origin": origin, "path": path, "port": proxy.port,
                              "enter": proxy.enter_path(path)})


async def h_sessions(request: web.Request) -> web.Response:
    """The live sessions, light: the page's session picker reads this rather
    than /api/sessions, which carries every record the daemon ever held."""
    out = []
    for s in request.app["manager"].list():
        try:
            status = s.status()
        except Exception:  # noqa: BLE001
            continue
        if status == "exited":
            continue
        sdef = getattr(s, "sdef", None)
        out.append({"name": getattr(sdef, "name", ""), "status": status,
                    "cwd": str(getattr(sdef, "cwd", "") or "")})
    return web.json_response({"sessions": out})


async def h_windows(request: web.Request) -> web.Response:
    hub = _hub(request)
    session = request.query.get("session", "")
    wins = hub.windows_of(session) if session else list(hub.windows.values())
    return web.json_response({"windows": [w.to_dict() for w in wins]})


async def h_ws(request: web.Request) -> web.StreamResponse:
    hub = _hub(request)
    wid = request.query.get("window", "")
    if not _WINDOW_ID.match(wid):
        return web.json_response({"error": "window must look like w-xxxx"}, status=400)
    ws = web.WebSocketResponse(heartbeat=30, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)
    old = hub.windows.get(wid)
    if old is not None:
        hub._drop(old)
        with contextlib.suppress(Exception):
            # 4000: the page reopens under a new id (a duplicated tab carries
            # the same ?w= in its address).
            await old.ws.close(code=4000, message=b"replaced")
    win = Window(id=wid, ws=ws)
    hub.windows[wid] = win
    request.app["websockets"].add(ws)
    try:
        async for msg in ws:
            if msg.type != WSMsgType.TEXT:
                if msg.type in (WSMsgType.ERROR, WSMsgType.CLOSE):
                    break
                continue
            try:
                data = json.loads(msg.data)
            except json.JSONDecodeError:
                continue
            if isinstance(data, dict):
                hub.receive(win, data)
    finally:
        request.app["websockets"].discard(ws)
        if hub.windows.get(wid) is win:
            hub._drop(win)
    return ws


async def h_command(request: web.Request) -> web.Response:
    try:
        body = await _json_body(request)
        session = str(body.get("session") or "")
        if not session:
            raise WebDebugError(400, "session is required")
        args = body.get("args") or {}
        if not isinstance(args, dict):
            raise WebDebugError(400, "args must be an object")
        try:
            timeout = float(body.get("timeout") or COMMAND_TIMEOUT)
        except (TypeError, ValueError):
            raise WebDebugError(400, "timeout must be a number") from None
        result = await _hub(request).command(
            session, str(body.get("op") or ""), args, window=str(body.get("window") or ""),
            timeout=timeout)
    except WebDebugError as exc:
        return _err(exc)
    return web.json_response(result)


async def h_image_upload(request: web.Request) -> web.Response:
    kind = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    suffix = IMAGE_TYPES.get(kind)
    if suffix is None:
        return web.json_response({"error": f"{kind or 'no Content-Type'} is not an accepted image"},
                                 status=415)
    folder = image_folder()
    folder.mkdir(parents=True, exist_ok=True)
    name = secrets.token_hex(10) + suffix
    dest = folder / name
    size = 0
    try:
        with open(dest, "wb") as fh:
            async for chunk in request.content.iter_chunked(64 * 1024):
                size += len(chunk)
                if size > MAX_IMAGE_BYTES:
                    raise WebDebugError(413, "the image is larger than 20 MB")
                fh.write(chunk)
    except WebDebugError as exc:
        with contextlib.suppress(OSError):
            dest.unlink()
        return _err(exc)
    if size == 0:
        with contextlib.suppress(OSError):
            dest.unlink()
        return web.json_response({"error": "the image is empty"}, status=400)
    return web.json_response({"id": name, "path": str(dest), "bytes": size})


async def h_image(request: web.Request) -> web.StreamResponse:
    try:
        path = image_path(request.match_info["name"])
    except WebDebugError as exc:
        return _err(exc)
    if not path.is_file():
        return web.json_response({"error": "no such image"}, status=404)
    return web.FileResponse(path)


def _deliver(app, name: str, text: str, image: Optional[Path]) -> bool:
    """Type ``text`` into session ``name`` the way a thread message goes in
    (threads_api.deliver): a live session whose harness takes images gets the
    picture pasted into its composer first; a held one is woken."""
    manager = app["manager"]
    try:
        session = manager.get(name)
        before, body = None, text
        if image is not None:
            from .. import harnesses as harness_registry
            from . import api as api_mod, opening_images
            entry = harness_registry.registry().get(getattr(session.sdef, "harness", "") or "")
            if entry and entry.takes_images:
                kind = {".png": "image/png", ".jpg": "image/jpeg", ".webp": "image/webp"}[image.suffix]
                before, body = api_mod._opening_image_step(
                    session, [opening_images.Image(image, kind)], text, what="web debug screenshot")
        if session.queue_delivery(body, **({"before": before} if before else {})):
            return True
    except Exception:  # noqa: BLE001 -- gone or not deliverable
        pass
    try:
        return bool(manager.wake_soon(name, by="webdebug", deliver=text))
    except Exception:  # noqa: BLE001
        log.info("webdebug: could not deliver to %s", name)
        return False


async def h_send(request: web.Request) -> web.Response:
    try:
        body = await _json_body(request)
        session = str(body.get("session") or "")
        window = str(body.get("window") or "")
        if not session:
            raise WebDebugError(400, "pick a session in the top bar first")
        if not _WINDOW_ID.match(window):
            raise WebDebugError(400, "window must look like w-xxxx")
        image = image_path(body["image"]) if body.get("image") else None
        if image is not None and not image.is_file():
            raise WebDebugError(400, "the image was not uploaded")
        if not (body.get("note") or body.get("element") or image):
            raise WebDebugError(400, "there is nothing to send")
        try:
            request.app["manager"].get(session)
        except Exception:  # noqa: BLE001
            raise WebDebugError(404, f"no session named {session!r}") from None
    except WebDebugError as exc:
        return _err(exc)
    text = compose_send(window, body, image)
    delivered = _deliver(request.app, session, text, image)
    return web.json_response({"delivered": delivered, "text": text})


async def _lifecycle(app):
    yield
    await app["webdebug"].stop_all()


def install(app: web.Application, hub: Optional[Hub] = None) -> None:
    app["webdebug"] = hub if hub is not None else Hub()
    app.cleanup_ctx.append(_lifecycle)
    r = app.router
    r.add_get("/webdebug", h_page)
    r.add_post("/api/webdebug/proxy", h_proxy)
    r.add_get("/api/webdebug/sessions", h_sessions)
    r.add_get("/api/webdebug/windows", h_windows)
    r.add_get("/api/webdebug/ws", h_ws)
    r.add_post("/api/webdebug/command", h_command)
    r.add_post("/api/webdebug/images", h_image_upload)
    r.add_get("/api/webdebug/images/{name}", h_image)
    r.add_post("/api/webdebug/send", h_send)
