"""MCP tools for the web debug page (claunch-o8pb7.2).

Thin clients of the daemon's ``/api/webdebug`` routes (daemon/webdebug.py).
The user opens a web debug window (the ``web`` button in a session's
terminal header), loads the page under development in it and picks which
session it is for; that session drives the window with these tools. Every
call carries ``$CLAUNCH_SESSION``, and the daemon refuses a window that is
showing another session.

The page runs in the daemon's own headless browser and the user watches its
screen, so a tool acts on a real page with real input behind it -- and a
screenshot is the browser's own capture, available whether or not anyone is
watching.
"""

from __future__ import annotations

import os
import urllib.parse

from . import daemon_client, mcp_rpc

_WINDOW = {"window": {"type": "string",
                      "description": "window id (w-xxxx); omitted: the window showing your session "
                                     "that the user focused last"}}
_TARGET = {
    "ref": {"type": "string", "description": "element ref from webdebug_snapshot (e12)"},
    "selector": {"type": "string", "description": "CSS selector (first match), instead of ref"},
}

TOOLS = [
    {"name": "webdebug_windows",
     "description": "The web debug windows the user opened for your session: id, the page's url and "
                    "title, its viewport, and how many screencast frames it has sent.",
     "inputSchema": {"type": "object", "properties": {}}},
    {"name": "webdebug_snapshot",
     "description": "Read the page in a web debug window: url, title, viewport, its visible interactive "
                    "and landmark elements one per line as '<ref> <role> \"<name>\" @x,y wxh' (refs feed "
                    "the other tools), the page text, and how many console errors it logged.",
     "inputSchema": {"type": "object", "properties": {
         **_WINDOW,
         "max": {"type": "integer", "description": "at most this many element lines (default 200)"},
         "text": {"type": "integer", "description": "characters of page text (default 3000)"}}}},
    {"name": "webdebug_query",
     "description": "Elements matching a CSS selector, with selector path, text, attributes, rect, "
                    "computed styles and outerHTML.",
     "inputSchema": {"type": "object", "properties": {
         **_WINDOW, "selector": {"type": "string"},
         "max": {"type": "integer", "description": "default 20"}}, "required": ["selector"]}},
    {"name": "webdebug_click",
     "description": "Click an element (pointer, mouse and click events at its centre). The user sees "
                    "it flashed in the page.",
     "inputSchema": {"type": "object", "properties": {**_WINDOW, **_TARGET}}},
    {"name": "webdebug_type",
     "description": "Type text into an input, textarea or contenteditable (replaces its value unless "
                    "clear=false; fires input/change, so React-style forms see it).",
     "inputSchema": {"type": "object", "properties": {
         **_WINDOW, **_TARGET, "text": {"type": "string"},
         "clear": {"type": "boolean", "description": "replace the current value (default true)"},
         "submit": {"type": "boolean", "description": "press Enter afterwards and submit its form"}},
         "required": ["text"]}},
    {"name": "webdebug_press",
     "description": "Press a key (keydown/keypress/keyup) on an element, or on the focused one.",
     "inputSchema": {"type": "object", "properties": {
         **_WINDOW, **_TARGET, "key": {"type": "string", "description": "e.g. Enter, Escape, ArrowDown"}},
         "required": ["key"]}},
    {"name": "webdebug_scroll",
     "description": "Scroll an element into view, or the page by x/y pixels.",
     "inputSchema": {"type": "object", "properties": {
         **_WINDOW, **_TARGET, "x": {"type": "integer"}, "y": {"type": "integer"}}}},
    {"name": "webdebug_navigate",
     "description": "Open an address in the window (as if the user typed it in its address bar) and "
                    "wait for it to load. Addresses are resolved on the daemon's machine.",
     "inputSchema": {"type": "object", "properties": {**_WINDOW, "url": {"type": "string"}},
                     "required": ["url"]}},
    {"name": "webdebug_eval",
     "description": "Run JavaScript in the page and return its JSON-able result (an expression, or a "
                    "function body using return; await works).",
     "inputSchema": {"type": "object", "properties": {**_WINDOW, "code": {"type": "string"}},
                     "required": ["code"]}},
    {"name": "webdebug_console",
     "description": "The page's console output and uncaught errors since it loaded (last 300 kept). "
                    "Pass since=<last> from a previous call to get only newer entries.",
     "inputSchema": {"type": "object", "properties": {
         **_WINDOW, "since": {"type": "integer"},
         "level": {"type": "string", "description": "comma list: log,info,warn,error,debug"},
         "max": {"type": "integer", "description": "default 100"},
         "clear": {"type": "boolean"}}}},
    {"name": "webdebug_screenshot",
     "description": "Screenshot the page (or one element) as the user sees it: the daemon's browser "
                    "captures it, so it works whether or not anyone is watching. Returns the PNG's "
                    "path on this machine -- open it to look.",
     "inputSchema": {"type": "object", "properties": {**_WINDOW, **_TARGET}}},
]

_OPS = {
    "webdebug_snapshot": "snapshot", "webdebug_query": "query", "webdebug_click": "click",
    "webdebug_type": "type", "webdebug_press": "press", "webdebug_scroll": "scroll",
    "webdebug_navigate": "navigate", "webdebug_eval": "eval", "webdebug_console": "console",
    "webdebug_screenshot": "screenshot",
}

#: navigate waits for a page load and eval may await; the rest answer at once.
_TIMEOUTS = {"navigate": 30, "eval": 30, "screenshot": 30}


class WebDebugMcpError(Exception):
    pass


def _client():
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        raise WebDebugMcpError(f"cannot reach the claunch daemon: {daemon_client.unreachable_reason(why)}")
    return client


def _session() -> str:
    session = os.environ.get("CLAUNCH_SESSION")
    if not session:
        raise WebDebugMcpError("no $CLAUNCH_SESSION -- web debug only works inside a managed claunch session")
    return session


def call_tool(name: str, args: dict) -> dict:
    me = _session()
    c = _client()
    if name == "webdebug_windows":
        return c.get("/api/webdebug/windows?" + urllib.parse.urlencode({"session": me}))
    op = _OPS.get(name)
    if op is None:
        raise WebDebugMcpError(f"unknown tool {name!r}")
    rest = {k: v for k, v in (args or {}).items() if k != "window" and v is not None}
    body = {"session": me, "op": op, "args": rest, "timeout": _TIMEOUTS.get(op, 20)}
    if args.get("window"):
        body["window"] = args["window"]
    # The HTTP wait outlasts the daemon's own wait for the page, so a slow
    # page comes back as the daemon's "did not answer", not a socket timeout.
    return c.post("/api/webdebug/command", body, timeout=body["timeout"] + 10)


SERVER = mcp_rpc.Server(
    name="claunch-webdebug",
    tools=tuple(TOOLS),
    dispatch=call_tool,
    errors=(WebDebugMcpError, daemon_client.DaemonClientError),
)
