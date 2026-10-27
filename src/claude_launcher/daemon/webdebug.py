"""The web debug page (claunch-o8pb7.2): a browser the daemon runs, and the
screen of it the user watches.

The page under development is opened **inside the daemon's own headless
Chromium**, and what the user sees is that browser's screen -- CDP
``Page.startScreencast`` frames pushed over the page's WebSocket. Nothing is
framed, so none of the failures of the framed design can happen: there is no
second port to reach (claunch-dsb28's relay gap), no ``X-Frame-Options`` to
strip, no mixed content between an https dashboard and an http listener, and a
redirect to another origin is just a navigation inside the same browser
(claunch-o8pb7: ``_location`` could not rewrite an absolute redirect, so the
frame left the proxy and the probe with it).

Three parts, all in this module:

* **The browser.** One Chromium process for the whole daemon, launched on
  first use, found through ``$CLAUNCH_CHROMIUM``, then the Playwright browser
  cache, then the system installations (:func:`find_browser`). Each window
  gets its own browser context, so cookies and storage of one window never
  reach another. It is stopped after the last window closes
  (:data:`IDLE_SECONDS`), or with the daemon.

  Pure CDP over the connection aiohttp already provides (``web/static/
  webdebug-agent.js`` is the page-side half). No Playwright: it would add a
  Python dependency pinned to a browser revision, and the daemon's event loop
  is the only thing that needs to drive the browser.

* **Windows.** A window is one browser page plus the viewer tab that shows it.
  The viewer attaches to ``/api/webdebug/ws?window=w-xxxx``; the daemon pushes
  JPEG frames down that socket (binary) and receives the user's mouse, wheel
  and key events back up (JSON), which it turns into ``Input.dispatch*``. The
  window says which claunch session it is showing; only that session's agent
  can drive it. A viewer that goes away for longer than
  :data:`VIEWER_GRACE` closes its page; a reload inside that window re-attaches
  to the same page, so a login in the browser survives it.

* **Commands.** A session's MCP tools (webdebug_mcp.py) post a command to
  ``/api/webdebug/command``; the daemon runs it in the window's page -- DOM
  reads and element-level actions through the injected agent script, input
  and screenshots through CDP. The tool names, arguments and answer shapes are
  unchanged from the framed design. The other direction is always the user's
  explicit act: a picked element or a screenshot goes to the session only when
  the user presses Send (``/api/webdebug/send``), typed into the session as a
  ``[claunch webdebug w-...]`` block.
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import glob
import json
import logging
import os
import re
import secrets
import shutil
import tempfile
import time
import urllib.parse
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, List, Optional

import aiohttp
from aiohttp import WSMsgType, web

from . import paths

log = logging.getLogger(__name__)

PAGE_FILE = Path(__file__).resolve().parent.parent / "web" / "static" / "webdebug.html"
AGENT_FILE = Path(__file__).resolve().parent.parent / "web" / "static" / "webdebug-agent.js"

COMMAND_TIMEOUT = 20.0
MAX_COMMAND_TIMEOUT = 120.0
MAX_IMAGE_BYTES = 20 * 1024 * 1024
IMAGE_TYPES = {"image/png": ".png", "image/jpeg": ".jpg", "image/webp": ".webp"}
_IMAGE_ID = re.compile(r"^[0-9a-f]{20}\.(png|jpg|webp)$")
_WINDOW_ID = re.compile(r"^w-[0-9a-z]{4,16}$")

PREFIX = "[claunch webdebug"

#: What the MCP tools may ask a window to do. The agent script implements the
#: DOM ones; navigate, screenshot and the input domain are the daemon's.
OPS = frozenset({
    "snapshot", "query", "click", "type", "press", "scroll", "navigate",
    "eval", "console", "screenshot", "highlight", "rect",
})
AGENT_OPS = frozenset({"snapshot", "query", "click", "type", "press", "scroll",
                       "eval", "console", "highlight", "rect"})

#: The viewport every window renders at. The screencast is capped to it, so a
#: frame's pixel size is the page's CSS size and a click in the viewer maps to
#: page coordinates by ratio alone.
VIEWPORT_WIDTH = 1280
VIEWPORT_HEIGHT = 800
DEFAULT_QUALITY = 60
MIN_QUALITY = 10
MAX_QUALITY = 90

#: Windows alive at once. One page costs ~1.3 GiB across the browser's whole
#: process tree (measured 2026-10-27, Chrome for Testing 156, headless, 1280x800,
#: streaming); opening one more closes the oldest.
MAX_WINDOWS = max(1, int(os.environ.get("CLAUNCH_WEBDEBUG_MAX_WINDOWS") or 1))
#: The browser is stopped this long after its last window closed.
IDLE_SECONDS = float(os.environ.get("CLAUNCH_WEBDEBUG_IDLE") or 300)
#: A viewer tab that goes away keeps its page this long (a reload comes back).
VIEWER_GRACE = 45.0
#: Frames are dropped rather than queued when a viewer lags this far behind.
MAX_WRITE_BUFFER = 4 * 1024 * 1024

BROWSER_ENV = "CLAUNCH_CHROMIUM"
#: A dev server on https with a self-signed certificate (or a proxy whose CA the
#: browser does not trust) fails every navigation with
#: ERR_CERT_AUTHORITY_INVALID. Turning this on makes the debug browser accept
#: any certificate; it is off by default because that browser then also trusts
#: whatever a real site's attacker presents.
INSECURE_TLS_ENV = "CLAUNCH_WEBDEBUG_INSECURE_TLS"
_LAUNCH_TIMEOUT = 25.0

_KEY_MODIFIERS = {"Alt": 1, "Control": 2, "Meta": 4, "Shift": 8}


class WebDebugError(Exception):
    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


def target_url(raw: str) -> str:
    """The address to open: a bare ``localhost:3000/x`` is read as http, and
    only what a browser can open is accepted. Addresses resolve on the
    daemon's machine -- ``localhost`` is the daemon's own."""
    text = str(raw or "").strip()
    if not text:
        raise WebDebugError(400, "an address is required")
    allowed = ("http", "https", "about", "data", "file")
    scheme = urllib.parse.urlsplit(text).scheme.lower()
    if scheme and scheme not in allowed:
        # ``example.com:8080/x`` reads as a scheme too; a port after the colon
        # (digits, then the path or nothing) means the user typed an address.
        rest = text.split(":", 1)[1]
        if not re.match(r"^\d+(/|$|\?)", rest):
            raise WebDebugError(400, f"only http and https pages can be opened, not {scheme}:")
        text = "http://" + text
    elif not scheme:
        text = "http://" + text
    return text


# --------------------------------------------------------------------------- #
# the browser
# --------------------------------------------------------------------------- #
def browser_candidates() -> List[str]:
    """Every browser this machine might have, best first.

    The environment wins; then the Playwright cache (a browser an earlier
    Playwright download left here -- reusing it avoids a 400 MB download and
    pins nothing); then what the distribution installed.
    """
    out: List[str] = []
    env = os.environ.get(BROWSER_ENV)
    if env:
        out.append(env)
    home = Path.home()
    for pattern in ("chromium-*/chrome-linux64/chrome", "chromium-*/chrome-linux/chrome",
                    "chromium_headless_shell-*/chrome-linux*/headless_shell"):
        out.extend(sorted(glob.glob(str(home / ".cache" / "ms-playwright" / pattern)), reverse=True))
    out.extend(["/usr/bin/chromium", "/usr/bin/chromium-browser", "/usr/bin/google-chrome",
                "/usr/bin/google-chrome-stable", "/snap/bin/chromium"])
    return [p for p in out if p]


def find_browser() -> str:
    for candidate in browser_candidates():
        path = Path(candidate).expanduser()
        if path.is_file() and os.access(path, os.X_OK):
            return str(path)
    raise WebDebugError(503, (
        "no Chromium on this machine for the web debug page. Install one, or point "
        f"${BROWSER_ENV} at its binary (looked at: "
        + ", ".join(browser_candidates()[:6]) + ")"))


class CDP:
    """One DevTools connection: numbered requests, events to a callback."""

    def __init__(self, ws: aiohttp.ClientWebSocketResponse,
                 on_event: Callable[[dict], None]) -> None:
        self.ws = ws
        self._on_event = on_event
        self._seq = 0
        self._pending: Dict[int, asyncio.Future] = {}
        self._write_lock = asyncio.Lock()
        self._task: Optional[asyncio.Task] = None
        self.closed = False

    def start(self) -> None:
        self._task = asyncio.create_task(self._pump())

    async def _pump(self) -> None:
        try:
            async for msg in self.ws:
                if msg.type != WSMsgType.TEXT:
                    if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR, WSMsgType.CLOSED):
                        break
                    continue
                try:
                    data = json.loads(msg.data)
                except json.JSONDecodeError:
                    continue
                if "id" in data:
                    fut = self._pending.pop(int(data["id"]), None)
                    if fut is not None and not fut.done():
                        fut.set_result(data)
                else:
                    try:
                        self._on_event(data)
                    except Exception:  # noqa: BLE001 -- an event must not kill the pump
                        log.exception("webdebug: event handler failed")
        finally:
            self.closed = True
            for fut in self._pending.values():
                if not fut.done():
                    fut.set_exception(ConnectionResetError("the browser connection closed"))
            self._pending.clear()

    async def send(self, method: str, params: Optional[dict] = None, *,
                   session: str = "", timeout: float = COMMAND_TIMEOUT) -> dict:
        if self.closed:
            raise WebDebugError(503, "the browser connection is closed")
        self._seq += 1
        mid = self._seq
        payload: Dict[str, object] = {"id": mid, "method": method, "params": params or {}}
        if session:
            payload["sessionId"] = session
        fut = asyncio.get_running_loop().create_future()
        self._pending[mid] = fut
        try:
            async with self._write_lock:
                await self.ws.send_json(payload)
            reply = await asyncio.wait_for(fut, timeout=timeout)
        except asyncio.TimeoutError:
            raise WebDebugError(504, f"the browser did not answer {method} within {timeout:g}s") from None
        except (ConnectionResetError, RuntimeError) as exc:
            raise WebDebugError(503, f"the browser went away: {exc}") from None
        finally:
            self._pending.pop(mid, None)
        error = reply.get("error")
        if error:
            raise WebDebugError(502, f"{method}: {error.get('message') or error}")
        return reply.get("result") or {}

    async def close(self) -> None:
        self.closed = True
        with contextlib.suppress(Exception):
            await self.ws.close()
        if self._task is not None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await asyncio.wait_for(self._task, timeout=5)


class Browser:
    """The Chromium process and its DevTools connection (one per daemon)."""

    def __init__(self, on_event: Callable[[dict], None]) -> None:
        self._on_event = on_event
        self._proc: Optional[asyncio.subprocess.Process] = None
        self._cdp: Optional[CDP] = None
        self._session: Optional[aiohttp.ClientSession] = None
        self._profile: Optional[str] = None
        self._lock = asyncio.Lock()
        self.path = ""

    @property
    def live(self) -> bool:
        return self._cdp is not None and not self._cdp.closed

    @property
    def cdp(self) -> Optional["CDP"]:
        return self._cdp

    async def ensure(self) -> CDP:
        async with self._lock:
            if self.live:
                return self._cdp  # type: ignore[return-value]
            await self._start_locked()
            return self._cdp  # type: ignore[return-value]

    async def _start_locked(self) -> None:
        await self._stop_locked()
        self.path = find_browser()
        # Not /tmp: that is one directory for the whole machine, and several
        # daemons (a test run's included) may have a browser at once. Under
        # the daemon's own home the profile is ours to name and to sweep.
        root = paths.daemon_dir() / "webdebug"
        root.mkdir(parents=True, exist_ok=True)
        for stale in root.glob("browser-*"):
            with contextlib.suppress(OSError):
                if time.time() - stale.stat().st_mtime > 3600:
                    shutil.rmtree(stale, ignore_errors=True)
        self._profile = tempfile.mkdtemp(prefix="browser-", dir=str(root))
        port_file = Path(self._profile) / "DevToolsActivePort"
        args = [
            self.path,
            "--headless=new",
            "--remote-debugging-port=0",
            f"--user-data-dir={self._profile}",
            f"--window-size={VIEWPORT_WIDTH},{VIEWPORT_HEIGHT}",
            "--no-first-run", "--no-default-browser-check", "--disable-background-networking",
            "--disable-gpu", "--hide-scrollbars", "--mute-audio", "--disable-dev-shm-usage",
            "--disable-extensions", "--disable-sync", "--no-sandbox",
            "about:blank",
        ]
        if os.environ.get(INSECURE_TLS_ENV) not in (None, "", "0", "false", "no"):
            args.insert(-1, "--ignore-certificate-errors")
        log.info("webdebug: starting browser %s", self.path)
        self._proc = await asyncio.create_subprocess_exec(
            *args, stdout=asyncio.subprocess.DEVNULL, stderr=asyncio.subprocess.DEVNULL)
        deadline = time.monotonic() + _LAUNCH_TIMEOUT
        port = 0
        while time.monotonic() < deadline:
            if self._proc.returncode is not None:
                raise WebDebugError(503, (
                    f"the browser {self.path} exited with {self._proc.returncode} before it "
                    "opened a DevTools port"))
            if port_file.is_file():
                text = port_file.read_text(errors="replace").split()
                if text:
                    port = int(text[0])
                    break
            await asyncio.sleep(0.05)
        if not port:
            raise WebDebugError(503, f"the browser {self.path} did not open a DevTools port "
                                     f"within {_LAUNCH_TIMEOUT:g}s")
        self._session = aiohttp.ClientSession()
        try:
            async with self._session.get(f"http://127.0.0.1:{port}/json/version") as resp:
                version = await resp.json()
        except (aiohttp.ClientError, OSError, asyncio.TimeoutError) as exc:
            raise WebDebugError(503, f"could not read the browser's DevTools endpoint: {exc}") from None
        ws = await self._session.ws_connect(version["webSocketDebuggerUrl"],
                                            max_msg_size=64 * 1024 * 1024, heartbeat=30)
        self._cdp = CDP(ws, self._on_event)
        self._cdp.start()
        log.info("webdebug: browser %s up (%s)", version.get("Browser"), self.path)

    async def stop(self) -> None:
        async with self._lock:
            await self._stop_locked()

    async def _stop_locked(self) -> None:
        cdp, self._cdp = self._cdp, None
        if cdp is not None:
            await cdp.close()
        proc, self._proc = self._proc, None
        if proc is not None and proc.returncode is None:
            proc.terminate()
            try:
                await asyncio.wait_for(proc.wait(), timeout=5)
            except asyncio.TimeoutError:
                proc.kill()
                with contextlib.suppress(Exception):
                    await proc.wait()
        session, self._session = self._session, None
        if session is not None:
            with contextlib.suppress(Exception):
                await session.close()
        profile, self._profile = self._profile, None
        if profile:
            shutil.rmtree(profile, ignore_errors=True)

    async def new_page(self, url: str = "about:blank") -> Dict[str, str]:
        cdp = await self.ensure()
        context = await cdp.send("Target.createBrowserContext", {"disposeOnDetach": False})
        context_id = context["browserContextId"]
        target = await cdp.send("Target.createTarget",
                                {"url": "about:blank", "browserContextId": context_id})
        attach = await cdp.send("Target.attachToTarget",
                                {"targetId": target["targetId"], "flatten": True})
        session_id = attach["sessionId"]
        await cdp.send("Page.enable", session=session_id)
        await cdp.send("Runtime.enable", session=session_id)
        await cdp.send("Log.enable", session=session_id)
        await cdp.send("Emulation.setDeviceMetricsOverride", {
            "width": VIEWPORT_WIDTH, "height": VIEWPORT_HEIGHT,
            "deviceScaleFactor": 1, "mobile": False,
        }, session=session_id)
        source = AGENT_FILE.read_text(encoding="utf-8") if AGENT_FILE.is_file() else ""
        if source:
            await cdp.send("Page.addScriptToEvaluateOnNewDocument",
                           {"source": source}, session=session_id)
        return {"context_id": context_id, "target_id": target["targetId"], "session_id": session_id}

    async def close_page(self, page: Dict[str, str]) -> None:
        cdp = self._cdp
        if cdp is None or cdp.closed:
            return
        with contextlib.suppress(WebDebugError, Exception):
            await cdp.send("Target.disposeBrowserContext",
                           {"browserContextId": page.get("context_id", "")}, timeout=5)


# --------------------------------------------------------------------------- #
# windows
# --------------------------------------------------------------------------- #
def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


@dataclass
class Window:
    id: str
    page: Dict[str, str]
    session: str = ""
    url: str = ""
    title: str = ""
    quality: int = DEFAULT_QUALITY
    viewers: List[web.WebSocketResponse] = field(default_factory=list)
    frame_size: tuple = (0, 0)
    last_frame: bytes = b""
    frames: int = 0
    opened_at: float = field(default_factory=time.time)
    focused_at: float = field(default_factory=time.time)
    closing: bool = False
    #: Resolved by the browser's Page.loadEventFired for this window, so a
    #: navigate waits for the page it asked for and not for the one before it.
    loading: Optional[asyncio.Future] = None

    @property
    def cdp_session(self) -> str:
        return self.page.get("session_id", "")

    def to_dict(self) -> dict:
        return {"window": self.id, "session": self.session, "url": self.url, "title": self.title,
                "screen_shared": True, "frames": self.frames,
                "viewport": {"width": self.frame_size[0], "height": self.frame_size[1]},
                "opened_at": _iso(self.opened_at), "focused_at": _iso(self.focused_at)}


class Hub:
    """The browser and the windows of one daemon."""

    def __init__(self) -> None:
        self.browser = Browser(self._event)
        self.windows: Dict[str, Window] = {}
        self._lock = asyncio.Lock()
        self._reapers: Dict[str, asyncio.Task] = {}
        self._idle: Optional[asyncio.Task] = None

    # -- lifecycle ------------------------------------------------------- #
    async def stop_all(self) -> None:
        for task in list(self._reapers.values()):
            task.cancel()
        self._reapers.clear()
        if self._idle is not None:
            self._idle.cancel()
            self._idle = None
        windows, self.windows = list(self.windows.values()), {}
        for win in windows:
            await self._close_page(win)
        await self.browser.stop()

    async def _close_page(self, win: Window) -> None:
        win.closing = True
        for ws in list(win.viewers):
            with contextlib.suppress(Exception):
                await ws.close(code=4001, message=b"the window closed")
        win.viewers.clear()
        await self.browser.close_page(win.page)

    def _schedule_idle(self) -> None:
        if self._idle is not None:
            self._idle.cancel()

        async def stop_later() -> None:
            try:
                await asyncio.sleep(IDLE_SECONDS)
                if not self.windows:
                    log.info("webdebug: no window for %gs, stopping the browser", IDLE_SECONDS)
                    await self.browser.stop()
            except asyncio.CancelledError:
                pass

        self._idle = asyncio.create_task(stop_later())

    # -- windows --------------------------------------------------------- #
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

    async def attach(self, window: str, session: str, ws: web.WebSocketResponse) -> Window:
        async with self._lock:
            win = self.windows.get(window)
            reaper = self._reapers.pop(window, None)
            if reaper is not None:
                reaper.cancel()
            if win is None:
                while len(self.windows) >= MAX_WINDOWS:
                    oldest = min(self.windows.values(), key=lambda w: w.opened_at)
                    log.info("webdebug: %d window(s) already open, closing %s", len(self.windows), oldest.id)
                    self.windows.pop(oldest.id, None)
                    await self._close_page(oldest)
                page = await self.browser.new_page()
                win = Window(id=window, page=page, session=session)
                self.windows[window] = win
            win.viewers.append(ws)
            if session:
                win.session = session
            if self._idle is not None:
                self._idle.cancel()
                self._idle = None
        await self.ensure_screencast(win)
        if win.last_frame:
            with contextlib.suppress(Exception):
                await ws.send_bytes(win.last_frame)
        await self.send_state(win)
        return win

    async def detach(self, win: Window, ws: web.WebSocketResponse) -> None:
        with contextlib.suppress(ValueError):
            win.viewers.remove(ws)
        if win.viewers or win.closing:
            return

        async def reap() -> None:
            try:
                await asyncio.sleep(VIEWER_GRACE)
            except asyncio.CancelledError:
                return
            if win.viewers or win.closing:
                return
            async with self._lock:
                if self.windows.get(win.id) is win:
                    self.windows.pop(win.id, None)
                    await self._close_page(win)
                self._reapers.pop(win.id, None)
            if not self.windows:
                self._schedule_idle()

        self._reapers[win.id] = asyncio.create_task(reap())

    async def ensure_screencast(self, win: Window, quality: int = 0) -> None:
        cdp = await self.browser.ensure()
        if quality and max(MIN_QUALITY, min(MAX_QUALITY, int(quality))) != win.quality:
            win.quality = max(MIN_QUALITY, min(MAX_QUALITY, int(quality)))
            # A stream already running keeps its encoder settings; changing the
            # quality means stopping it and starting again.
            with contextlib.suppress(WebDebugError):
                await cdp.send("Page.stopScreencast", session=win.cdp_session, timeout=10)
        with contextlib.suppress(WebDebugError):
            await cdp.send("Page.startScreencast", {
                "format": "jpeg", "quality": win.quality,
                "maxWidth": VIEWPORT_WIDTH, "maxHeight": VIEWPORT_HEIGHT,
                "everyNthFrame": 1,
            }, session=win.cdp_session, timeout=10)

    # -- events from the browser ------------------------------------------ #
    def _event(self, data: dict) -> None:
        method = data.get("method")
        session_id = data.get("sessionId") or ""
        params = data.get("params") or {}
        win = self._win_of(session_id)
        if win is None:
            return
        if method == "Page.screencastFrame":
            asyncio.create_task(self._on_frame(win, params))
        elif method == "Page.frameNavigated":
            frame = params.get("frame") or {}
            if not frame.get("parentId"):
                win.url = str(frame.get("url") or "")[:2000]
                if win.url and win.url != "about:blank":
                    asyncio.create_task(self.send_state(win))
        elif method == "Page.navigatedWithinDocument":
            win.url = str(params.get("url") or "")[:2000]
            asyncio.create_task(self.send_state(win))
        elif method == "Page.loadEventFired":
            waiter, win.loading = win.loading, None
            if waiter is not None and not waiter.done():
                waiter.set_result(True)
            asyncio.create_task(self._refresh_title(win))
            asyncio.create_task(self.ensure_screencast(win))

    def _win_of(self, session_id: str) -> Optional[Window]:
        for win in self.windows.values():
            if win.cdp_session == session_id:
                return win
        return None

    async def _on_frame(self, win: Window, params: dict) -> None:
        data = params.get("data") or ""
        metadata = params.get("metadata") or {}
        cdp = self.browser.cdp
        if cdp is not None:
            with contextlib.suppress(WebDebugError, Exception):
                await cdp.send("Page.screencastFrameAck", {"sessionId": params.get("sessionId")},
                               session=win.cdp_session, timeout=5)
        try:
            jpeg = base64.b64decode(data)
        except (ValueError, TypeError):
            return
        win.last_frame = jpeg
        win.frames += 1
        size = (int(metadata.get("deviceWidth") or 0), int(metadata.get("deviceHeight") or 0))
        if size[0] and size != win.frame_size:
            win.frame_size = size
        for ws in list(win.viewers):
            transport = getattr(getattr(ws, "_req", None), "transport", None)
            if transport is not None and transport.get_write_buffer_size() > MAX_WRITE_BUFFER:
                continue  # a slow viewer loses frames instead of growing the queue
            try:
                await ws.send_bytes(jpeg)
            except (ConnectionResetError, RuntimeError):
                with contextlib.suppress(ValueError):
                    win.viewers.remove(ws)

    async def _refresh_title(self, win: Window) -> None:
        try:
            value = await self.evaluate(win, "JSON.stringify({url: location.href, title: document.title})",
                                        timeout=5)
            state = json.loads(value) if isinstance(value, str) else {}
        except (WebDebugError, ValueError):
            return
        win.url = str(state.get("url") or win.url)[:2000]
        win.title = str(state.get("title") or "")[:2000]
        await self.send_state(win)

    async def send_state(self, win: Window) -> None:
        payload = {"type": "state", "window": win.id, "session": win.session, "url": win.url,
                   "title": win.title, "frames": win.frames,
                   "viewport": {"width": win.frame_size[0] or VIEWPORT_WIDTH,
                                "height": win.frame_size[1] or VIEWPORT_HEIGHT}}
        for ws in list(win.viewers):
            with contextlib.suppress(Exception):
                await ws.send_json(payload)

    async def broadcast(self, win: Window, payload: dict) -> None:
        for ws in list(win.viewers):
            with contextlib.suppress(Exception):
                await ws.send_json(payload)

    # -- running things in the page --------------------------------------- #
    async def evaluate(self, win: Window, expression: str, *, timeout: float = COMMAND_TIMEOUT,
                       await_promise: bool = False) -> object:
        cdp = await self.browser.ensure()
        result = await cdp.send("Runtime.evaluate", {
            "expression": expression, "returnByValue": True, "awaitPromise": await_promise,
            "userGesture": True,
        }, session=win.cdp_session, timeout=timeout)
        if result.get("exceptionDetails"):
            details = result["exceptionDetails"]
            message = details.get("exception", {}).get("description") or details.get("text") or "failed"
            raise WebDebugError(422, str(message).strip().splitlines()[0][:1000])
        return (result.get("result") or {}).get("value")

    async def agent(self, win: Window, op: str, args: dict, *, timeout: float = COMMAND_TIMEOUT) -> object:
        expr = ("window.__claunchWebdebug && window.__claunchWebdebug.op("
                + json.dumps(op) + ", " + json.dumps(args or {}) + ")")
        try:
            return await self.evaluate(win, expr, timeout=timeout, await_promise=True)
        except WebDebugError as exc:
            if "is not defined" not in str(exc) or "Promise" in str(exc):
                raise
            # The document predates the injection (about:blank, or a page opened
            # before the agent existed): install it once, then retry.
            source = AGENT_FILE.read_text(encoding="utf-8") if AGENT_FILE.is_file() else ""
            if not source:
                raise
            await self.evaluate(win, source, timeout=timeout)
            return await self.evaluate(win, expr, timeout=timeout, await_promise=True)

    async def navigate(self, win: Window, url: str, *, timeout: float = 30) -> dict:
        target = target_url(url)
        cdp = await self.browser.ensure()
        loaded = asyncio.get_running_loop().create_future()
        win.loading = loaded
        try:
            result = await cdp.send("Page.navigate", {"url": target},
                                    session=win.cdp_session, timeout=timeout)
            if result.get("errorText"):
                raise WebDebugError(502, f"the browser refused {target}: {result['errorText']}")
            with contextlib.suppress(asyncio.TimeoutError):
                await asyncio.wait_for(asyncio.shield(loaded), timeout=timeout)
        finally:
            if win.loading is loaded and not loaded.done():
                win.loading = None
        win.url = str(result.get("url") or target)[:2000]
        await self._refresh_title(win)
        await self.ensure_screencast(win)
        return {"url": win.url, "title": win.title}

    async def input_event(self, win: Window, event: dict) -> None:
        cdp = await self.browser.ensure()
        kind = str(event.get("kind") or "")
        modifiers = int(event.get("modifiers") or 0)
        if kind == "mouse":
            what = str(event.get("type") or "mouseMoved")
            params: Dict[str, object] = {
                "type": what, "x": float(event.get("x") or 0), "y": float(event.get("y") or 0),
                "modifiers": modifiers,
            }
            if what == "mouseWheel":
                params["deltaX"] = float(event.get("deltaX") or 0)
                params["deltaY"] = float(event.get("deltaY") or 0)
            else:
                params["button"] = str(event.get("button") or "none")
                params["buttons"] = int(event.get("buttons") or 0)
                params["clickCount"] = int(event.get("clickCount") or (1 if what in ("mousePressed", "mouseReleased") else 0))
            await cdp.send("Input.dispatchMouseEvent", params, session=win.cdp_session)
        elif kind == "key":
            what = "keyDown" if str(event.get("type")) == "keyDown" else "keyUp"
            key = str(event.get("key") or "")
            params = {"type": what, "key": key, "code": str(event.get("code") or ""),
                      "modifiers": modifiers, "windowsVirtualKeyCode": int(event.get("keyCode") or 0)}
            text = event.get("text")
            if what == "keyDown" and text:
                params["text"] = str(text)[:8]
            await cdp.send("Input.dispatchKeyEvent", params, session=win.cdp_session)
        elif kind == "text":
            await cdp.send("Input.insertText", {"text": str(event.get("text") or "")[:4000]},
                           session=win.cdp_session)
        elif kind == "focus":
            await cdp.send("Page.bringToFront", session=win.cdp_session)

    # -- commands from MCP ------------------------------------------------ #
    async def command(self, session: str, op: str, args: dict, *, window: str = "",
                      timeout: float = COMMAND_TIMEOUT) -> dict:
        if op not in OPS:
            raise WebDebugError(400, f"unknown operation {op!r}")
        win = self.window_for(session, window)
        wait = max(1.0, min(timeout, MAX_COMMAND_TIMEOUT))
        win.focused_at = time.time()
        if op == "navigate":
            value = await self.navigate(win, str(args.get("url") or ""), timeout=max(5.0, wait))
        elif op == "screenshot":
            value = await self.screenshot(win, args, timeout=wait)
        elif op in AGENT_OPS:
            value = await self.agent(win, op, args, timeout=wait)
        else:
            raise WebDebugError(400, f"unknown operation {op!r}")
        if isinstance(value, dict) and value.get("image"):
            value = dict(value)
            value["image"] = str(image_path(value["image"]))
        await self.broadcast(win, {"type": "activity", "text": f"agent: {op}"})
        return {"window": win.id, "op": op, "url": win.url, "value": value}

    async def screenshot(self, win: Window, args: dict, *, timeout: float = 30) -> dict:
        cdp = await self.browser.ensure()
        params: Dict[str, object] = {"format": "png", "captureBeyondViewport": False}
        element = None
        if args.get("ref") or args.get("selector"):
            info = await self.agent(win, "rect", {"ref": args.get("ref"),
                                                  "selector": args.get("selector")}, timeout=timeout)
            rect = (info or {}).get("rect") or {}
            element = (info or {}).get("element")
            if rect.get("width") and rect.get("height"):
                params["clip"] = {"x": max(0, rect["x"]), "y": max(0, rect["y"]),
                                  "width": rect["width"], "height": rect["height"], "scale": 1}
        shot = await cdp.send("Page.captureScreenshot", params,
                              session=win.cdp_session, timeout=timeout)
        data = base64.b64decode(shot.get("data") or "")
        if not data:
            raise WebDebugError(502, "the browser returned an empty screenshot")
        folder = image_folder()
        folder.mkdir(parents=True, exist_ok=True)
        name = secrets.token_hex(10) + ".png"
        (folder / name).write_bytes(data)
        out = {"image": name, "bytes": len(data), "url": win.url, "title": win.title}
        if element:
            out["element"] = element
        return out


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
        "for its log, webdebug_screenshot to capture what the user is looking at.")
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
        return web.Response(status=404, text="web debug page assets are missing")
    return web.FileResponse(PAGE_FILE, headers={"Cache-Control": "no-cache"})


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
    session = request.query.get("session", "")
    first_url = request.query.get("url", "")
    ws = web.WebSocketResponse(heartbeat=20, max_msg_size=4 * 1024 * 1024)
    await ws.prepare(request)
    try:
        win = await hub.attach(wid, session, ws)
    except WebDebugError as exc:
        with contextlib.suppress(Exception):
            await ws.send_json({"type": "error", "message": str(exc)})
            await ws.close()
        return ws
    request.app["websockets"].add(ws)
    if first_url:
        asyncio.create_task(_open_quietly(hub, win, first_url))
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
            if not isinstance(data, dict):
                continue
            await _viewer_message(hub, win, data)
    finally:
        request.app["websockets"].discard(ws)
        await hub.detach(win, ws)
    return ws


async def _open_quietly(hub: Hub, win: Window, url: str) -> None:
    try:
        await hub.navigate(win, url)
    except WebDebugError as exc:
        await hub.broadcast(win, {"type": "error", "message": str(exc)})


async def _viewer_message(hub: Hub, win: Window, msg: dict) -> None:
    kind = str(msg.get("type") or "")
    win.focused_at = time.time()
    try:
        if kind in ("hello", "session"):
            session = str(msg.get("session") or "")
            if session != win.session:
                win.session = session
            await hub.send_state(win)
        elif kind == "navigate":
            await hub.navigate(win, str(msg.get("url") or ""))
        elif kind == "input" and isinstance(msg.get("event"), dict):
            await hub.input_event(win, msg["event"])
        elif kind == "pick":
            value = await hub.agent(win, "pickAt", {"x": msg.get("x"), "y": msg.get("y")})
            await hub.broadcast(win, {"type": "picked", **(value or {})})
        elif kind == "quality":
            await hub.ensure_screencast(win, int(msg.get("quality") or 0))
        elif kind == "reload":
            await hub.navigate(win, win.url or "about:blank")
        elif kind == "back":
            await hub.evaluate(win, "history.back()")
        elif kind == "forward":
            await hub.evaluate(win, "history.forward()")
        elif kind == "snapshot":
            value = await hub.agent(win, "snapshot", {"max": 200})
            await hub.broadcast(win, {"type": "snapshot", "value": value})
        elif kind == "console":
            value = await hub.agent(win, "console", {"max": 100})
            await hub.broadcast(win, {"type": "console", "value": value})
        elif kind == "screenshot":
            value = await hub.screenshot(win, {"ref": msg.get("ref"), "selector": msg.get("selector")})
            await hub.broadcast(win, {"type": "screenshot", **value})
    except WebDebugError as exc:
        await hub.broadcast(win, {"type": "error", "message": str(exc)})


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
    r.add_get("/api/webdebug/sessions", h_sessions)
    r.add_get("/api/webdebug/windows", h_windows)
    r.add_get("/api/webdebug/ws", h_ws)
    r.add_post("/api/webdebug/command", h_command)
    r.add_post("/api/webdebug/images", h_image_upload)
    r.add_get("/api/webdebug/images/{name}", h_image)
    r.add_post("/api/webdebug/send", h_send)
