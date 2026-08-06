"""The daemon's HTTP surface: REST API, auth, and the static web UI.

Auth model: every ``/api/*`` call (except ``/api/health``) needs either the
Bearer token (CLI, scripts) or the HttpOnly cookie minted by
``POST /api/auth/session`` (browser — the SPA asks the user to paste the token
once). Tokens never travel in URLs, so they cannot leak into access logs or
browser history.
"""

from __future__ import annotations

import asyncio
import secrets
import time
from pathlib import Path

from aiohttp import web

from .. import __version__, profile as profile_mod
from ..cflow import engine as cflow_engine, state as cflow_state
from ..cflow.engine import CflowError
from ..cflow.model import WorkflowError
from ..cflow.state import StateError
from ..profile import ProfileError
from .harness import HarnessError, SessionDef
from .manager import ManagerError, SessionManager
from .session import SessionGone
from . import ws as ws_mod

COOKIE_NAME = "claunch_session"

_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"


def json_error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except (ManagerError, HarnessError, ProfileError) as exc:
        return json_error(400, str(exc))
    except SessionGone as exc:
        return json_error(409, str(exc))
    except web.HTTPException:
        raise


def build_auth_middleware(token: str, cookie_sessions: set):
    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        path = request.path
        if not path.startswith("/api/") or path == "/api/health" or path == "/api/auth/session":
            return await handler(request)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and secrets.compare_digest(auth[7:], token):
            return await handler(request)
        cookie = request.cookies.get(COOKIE_NAME)
        if cookie and cookie in cookie_sessions:
            return await handler(request)
        return json_error(401, "authentication required")

    return auth_middleware


def build_app(manager: SessionManager, token: str, *, started_at: float) -> web.Application:
    cookie_sessions: set = set()
    app = web.Application(
        middlewares=[error_middleware, build_auth_middleware(token, cookie_sessions)]
    )
    app["manager"] = manager
    app["token"] = token
    app["cookie_sessions"] = cookie_sessions
    app["started_at"] = started_at
    app["shutdown_event"] = asyncio.Event()

    r = app.router
    r.add_get("/api/health", h_health)
    r.add_post("/api/auth/session", h_auth_session)
    r.add_get("/api/daemon", h_daemon_info)
    r.add_post("/api/daemon/shutdown", h_daemon_shutdown)
    r.add_get("/api/profiles", h_profiles)
    r.add_get("/api/cflow", h_cflow_runs)
    r.add_get("/api/sessions", h_sessions_list)
    r.add_post("/api/sessions", h_sessions_create)
    r.add_get("/api/sessions/{name}", h_session_get)
    r.add_delete("/api/sessions/{name}", h_session_delete)
    r.add_post("/api/sessions/{name}/keys", h_session_keys)
    r.add_get("/api/sessions/{name}/capture", h_session_capture)
    r.add_get("/api/sessions/{name}/wait", h_session_wait)
    r.add_post("/api/sessions/{name}/resize", h_session_resize)
    r.add_get("/api/sessions/{name}/ws", ws_mod.terminal_ws)
    r.add_get("/", h_index)
    if _STATIC_DIR.is_dir():
        r.add_static("/static", _STATIC_DIR)
    return app


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
async def h_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "version": __version__})


async def h_auth_session(request: web.Request) -> web.Response:
    body = await _json_body(request)
    supplied = str(body.get("token") or "")
    if not secrets.compare_digest(supplied, request.app["token"]):
        return json_error(401, "bad token")
    session_id = secrets.token_urlsafe(32)
    request.app["cookie_sessions"].add(session_id)
    resp = web.json_response({"ok": True})
    resp.set_cookie(
        COOKIE_NAME, session_id, httponly=True, samesite="Strict", path="/"
    )
    return resp


async def h_daemon_info(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    return web.json_response(
        {
            "version": __version__,
            "uptime": round(time.monotonic() - request.app["started_at"], 1),
            "sessions": len(manager.list()),
        }
    )


async def h_daemon_shutdown(request: web.Request) -> web.Response:
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, request.app["shutdown_event"].set)
    return web.json_response({"ok": True})


async def h_profiles(request: web.Request) -> web.Response:
    return web.json_response({"profiles": [p.name for p in profile_mod.list_all()]})


#: Recent step reports included per run in the /api/cflow payload.
_CFLOW_REPORT_TAIL = 10


async def h_cflow_runs(request: web.Request) -> web.Response:
    """cflow runs in the directories of managed sessions (plus ``?cwd=``).

    A cflow run lives in a working directory, not in a session — but the
    sessions the daemon manages are where agent runs happen, so their cwds
    are the natural monitoring scope. ``?cwd=`` inspects any explicit
    directory (reported even when idle).
    """
    manager: SessionManager = request.app["manager"]
    by_cwd: dict = {}
    for session in manager.list():
        by_cwd.setdefault(session.sdef.cwd, []).append(session.sdef.name)
    explicit = request.query.get("cwd")
    if explicit:
        by_cwd.setdefault(explicit, [])

    runs = []
    for cwd, names in by_cwd.items():
        entry = {"cwd": cwd, "sessions": names}
        try:
            payload = cflow_engine.status(cwd)
        except (CflowError, WorkflowError, StateError, OSError) as exc:
            runs.append({**entry, "status": "error", "error": str(exc)})
            continue
        if payload.get("status") == "idle" and cwd != explicit:
            continue
        reports = [
            {
                "step": e.get("step"),
                "visit": e.get("visit"),
                "summary": e.get("summary"),
                "details": e.get("details"),
                "at": e.get("at"),
            }
            for e in cflow_state.read_journal(cwd, run_id=payload.get("run"))
            if e.get("event") == "step_report"
        ]
        runs.append({**entry, **payload, "reports": reports[-_CFLOW_REPORT_TAIL:]})
    return web.json_response({"runs": runs})


async def h_sessions_list(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    return web.json_response({"sessions": [s.info() for s in manager.list()]})


async def h_sessions_create(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    body = await _json_body(request)
    body.setdefault("restore", manager.restore_default)
    body.setdefault("name", "")
    try:
        sdef = SessionDef.from_dict(body)
    except (KeyError, ValueError, TypeError) as exc:
        return json_error(400, f"bad session definition: {exc}")
    try:
        session = manager.create(sdef)
    except ManagerError as exc:
        if "already exists" in str(exc):
            return json_error(409, str(exc))
        raise
    return web.json_response(session.info(), status=201)


def _session(request: web.Request):
    manager: SessionManager = request.app["manager"]
    return manager.get(request.match_info["name"])


async def h_session_get(request: web.Request) -> web.Response:
    return web.json_response(_session(request).info())


async def h_session_delete(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    force = request.query.get("force") in ("1", "true")
    session = manager.kill(request.match_info["name"], force=force)
    return web.json_response(session.info())


async def h_session_keys(request: web.Request) -> web.Response:
    session = _session(request)
    body = await _json_body(request)
    keys = body.get("keys")
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        return json_error(400, "'keys' must be a list of strings")
    data = await session.send_keys(keys, literal=bool(body.get("literal")))
    return web.json_response({"ok": True, "bytes": len(data)})


async def h_session_capture(request: web.Request) -> web.Response:
    session = _session(request)
    history = request.query.get("history") in ("1", "true")
    trim = request.query.get("trim", "1") not in ("0", "false")
    lines = session.capture(history=history)
    if trim:
        while lines and not lines[-1]:
            lines.pop()
    if request.query.get("format") == "json":
        x, y = session.screen.cursor()
        return web.json_response(
            {"lines": lines, "cursor": {"x": x, "y": y}, "status": session.status()}
        )
    text = "\n".join(lines)
    return web.Response(text=text + ("\n" if text else ""), content_type="text/plain")


async def h_session_wait(request: web.Request) -> web.Response:
    session = _session(request)
    state = request.query.get("state", "idle")
    if state not in ("idle", "exited"):
        return json_error(400, "state must be 'idle' or 'exited'")
    try:
        timeout = float(request.query.get("timeout", 30.0))
        threshold = float(request.query.get("threshold", session.idle_threshold))
    except ValueError:
        return json_error(400, "timeout/threshold must be numbers")
    try:
        final = await session.wait_for(state, timeout=timeout, threshold=threshold)
    except asyncio.TimeoutError:
        return web.json_response({"timeout": True, "status": session.status()}, status=408)
    return web.json_response({**session.info(), "timeout": False, "status": final})


async def h_session_resize(request: web.Request) -> web.Response:
    session = _session(request)
    body = await _json_body(request)
    try:
        cols, rows = int(body["cols"]), int(body["rows"])
    except (KeyError, ValueError, TypeError):
        return json_error(400, "'cols' and 'rows' must be integers")
    session.resize(cols, rows)
    return web.json_response({"ok": True})


async def h_index(request: web.Request) -> web.Response:
    index = _STATIC_DIR / "index.html"
    if not index.is_file():
        return web.Response(text="claunch daemon is running (web UI assets missing)")
    return web.FileResponse(index)


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}
