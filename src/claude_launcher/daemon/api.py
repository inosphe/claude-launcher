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


def _token_eq(supplied: str, expected: str) -> bool:
    # compare_digest rejects non-ASCII *strings* with a TypeError (a pasted
    # wrong token must yield 401, not a 500) — compare bytes instead.
    return secrets.compare_digest(
        supplied.encode("utf-8"), expected.encode("utf-8")
    )


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except (
        ManagerError,
        HarnessError,
        ProfileError,
        CflowError,
        WorkflowError,
        StateError,
    ) as exc:
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
        if auth.startswith("Bearer ") and _token_eq(auth[7:], token):
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
    app["websockets"] = set()
    # Open terminal sockets never close on their own; without this, runner
    # cleanup waits its shutdown timeout for every browser tab left open.
    app.on_shutdown.append(_close_websockets)

    r = app.router
    r.add_get("/api/health", h_health)
    r.add_post("/api/auth/session", h_auth_session)
    r.add_get("/api/daemon", h_daemon_info)
    r.add_post("/api/daemon/shutdown", h_daemon_shutdown)
    r.add_get("/api/profiles", h_profiles)
    r.add_get("/api/cflow", h_cflow_runs)
    r.add_get("/api/cflow/run", h_cflow_run_detail)
    r.add_post("/api/cflow/approve", h_cflow_approve)
    r.add_post("/api/cflow/select", h_cflow_select)
    r.add_post("/api/cflow/nudge", h_cflow_nudge)
    r.add_post("/api/cflow/goto", h_cflow_goto)
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


async def _close_websockets(app: web.Application) -> None:
    from aiohttp import WSCloseCode

    for ws in set(app["websockets"]):
        try:
            await ws.close(code=WSCloseCode.GOING_AWAY, message=b"daemon shutdown")
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
async def h_health(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok", "version": __version__})


async def h_auth_session(request: web.Request) -> web.Response:
    body = await _json_body(request)
    supplied = str(body.get("token") or "")
    if not _token_eq(supplied, request.app["token"]):
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
    """All monitorable cflow runs: the machine-local run registry (every
    directory a run was started in), plus the cwds of managed sessions
    (annotated with their session names), plus an explicit ``?cwd=``
    (reported even when idle).
    """
    manager: SessionManager = request.app["manager"]
    by_cwd: dict = {}
    for session in manager.list():
        key = str(Path(session.sdef.cwd).resolve())
        by_cwd.setdefault(key, []).append(session.sdef.name)
    for run_dir in cflow_state.known_run_dirs():
        by_cwd.setdefault(run_dir, [])
    explicit = request.query.get("cwd")
    if explicit:
        explicit = str(Path(explicit).resolve())
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


def _sessions_in(manager: SessionManager, cwd: str) -> list:
    return [
        s.sdef.name
        for s in manager.list()
        if str(Path(s.sdef.cwd).resolve()) == cwd
    ]


def _serialize_workflow(wf) -> dict:
    steps = []
    for s in wf.steps.values():
        entry = {
            "id": s.id,
            "title": s.title,
            "gate": s.gate,
            "verify": s.verify.command if s.verify else None,
            "next": s.next,
            "select": None,
        }
        if s.select:
            entry["select"] = {
                "prompt": s.select.prompt,
                "chooser": s.select.chooser,
                "options": [
                    {"name": o.name, "description": o.description, "next": o.next}
                    for o in s.select.options.values()
                ],
            }
        steps.append(entry)
    return {
        "name": wf.name,
        "description": wf.description,
        "start": wf.start,
        "max_visits": wf.max_visits,
        "warnings": wf.warnings,
        "steps": steps,
    }


async def h_cflow_run_detail(request: web.Request) -> web.Response:
    """Everything the dashboard's run page needs: live status, the full
    workflow graph, the step reports, and the run journal."""
    raw = request.query.get("cwd")
    if not raw:
        return json_error(400, "'cwd' query parameter required")
    cwd = str(Path(raw).resolve())
    manager: SessionManager = request.app["manager"]
    payload = cflow_engine.status(cwd)
    if payload.get("status") == "idle":
        return web.json_response(
            {"cwd": cwd, "status": "idle", "sessions": _sessions_in(manager, cwd)}
        )
    workflow = _serialize_workflow(cflow_state.load_snapshot(cwd))
    journal = cflow_state.read_journal(cwd, run_id=payload.get("run"))
    reports = [
        {
            "step": e.get("step"),
            "visit": e.get("visit"),
            "summary": e.get("summary"),
            "details": e.get("details"),
            "at": e.get("at"),
        }
        for e in journal
        if e.get("event") == "step_report"
    ]
    return web.json_response(
        {
            "cwd": cwd,
            "sessions": _sessions_in(manager, cwd),
            "run": payload,
            "workflow": workflow,
            "reports": reports,
            "journal": journal[-200:],
        }
    )


async def _cflow_action_cwd(request: web.Request):
    body = await _json_body(request)
    raw = str(body.get("cwd") or "")
    if not raw:
        return None, json_error(400, "'cwd' required in the JSON body")
    return (str(Path(raw).resolve()), body), None


async def _nudge_sessions(manager: SessionManager, cwd: str, message: str) -> list:
    """Type a resume nudge into the live managed sessions in this directory,
    so an agent that stopped its turn at the gate picks the run back up."""
    nudged = []
    for session in manager.list():
        if session.exited or str(Path(session.sdef.cwd).resolve()) != cwd:
            continue
        try:
            await session.send_keys([message, "Enter"])
        except Exception:
            continue
        nudged.append(session.sdef.name)
    return nudged


async def h_cflow_approve(request: web.Request) -> web.Response:
    """Approve the current gate / extend the loop limit — a human acting
    through the authenticated dashboard, same trust channel as the CLI.
    (Deliberately still not reachable by the agent: the MCP surface has no
    approve, and agents have no dashboard token.)"""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, _ = resolved
    payload = cflow_engine.approve(by="web", cwd=cwd)
    if payload.get("status") == "approved":
        payload["nudged_sessions"] = await _nudge_sessions(
            request.app["manager"], cwd, cflow_engine.NUDGE_APPROVED
        )
    return web.json_response(payload)


async def h_cflow_select(request: web.Request) -> web.Response:
    """Confirm (or override) a user-chooser branch from the dashboard."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, body = resolved
    option = str(body.get("option") or "")
    if not option:
        return json_error(400, "'option' required in the JSON body")
    reason = str(body.get("reason") or "") or None
    payload = cflow_engine.select(option, reason, by="web", cwd=cwd)
    if payload.get("status") == "selected":
        payload["nudged_sessions"] = await _nudge_sessions(
            request.app["manager"], cwd, cflow_engine.NUDGE_SELECTED
        )
    return web.json_response(payload)


async def h_cflow_nudge(request: web.Request) -> web.Response:
    """Manually (re-)nudge the run directory's sessions from the dashboard —
    for when an auto-nudge was missed, or the agent simply stalled."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, _ = resolved
    nudged = await _nudge_sessions(
        request.app["manager"], cwd, cflow_engine.NUDGE_CONTINUE
    )
    return web.json_response({"ok": True, "nudged_sessions": nudged})


async def h_cflow_goto(request: web.Request) -> web.Response:
    """Force the run's current step (human override), then nudge the
    directory's sessions so the agent continues from the new position."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, body = resolved
    step = str(body.get("step") or "")
    if not step:
        return json_error(400, "'step' required in the JSON body")
    reason = str(body.get("reason") or "") or None
    payload = cflow_engine.goto(step, by="web", reason=reason, cwd=cwd)
    payload["nudged_sessions"] = await _nudge_sessions(
        request.app["manager"], cwd, cflow_engine.nudge_for_state(step)
    )
    return web.json_response(payload)


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
