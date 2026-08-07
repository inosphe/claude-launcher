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
from ..cflow import engine as cflow_engine, model as cflow_model, state as cflow_state
from ..cflow.engine import CflowError
from ..cflow.model import WorkflowError
from ..cflow.state import StateError
from ..profile import ProfileError
from .harness import HarnessError, SessionDef
from .manager import ManagerError, SessionManager
from .mesh import MeshConflict, MeshError, MeshManager
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
    except (SessionGone, MeshConflict) as exc:
        return json_error(409, str(exc))
    except (
        ManagerError,
        HarnessError,
        ProfileError,
        CflowError,
        WorkflowError,
        StateError,
        MeshError,
    ) as exc:
        return json_error(400, str(exc))
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


def _relay_unconfigured() -> dict:
    return {"configured": False, "connected": False, "name": None}


def build_app(
    manager: SessionManager,
    token: str,
    *,
    started_at: float,
    mesh: MeshManager | None = None,
    relay_state=None,
) -> web.Application:
    cookie_sessions: set = set()
    app = web.Application(
        middlewares=[error_middleware, build_auth_middleware(token, cookie_sessions)]
    )
    app["manager"] = manager
    app["mesh"] = mesh if mesh is not None else MeshManager(manager)
    app["relay_state"] = relay_state if relay_state is not None else _relay_unconfigured
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
    r.add_get("/api/cflow/workflows", h_cflow_workflows)
    r.add_post("/api/cflow/start", h_cflow_start)
    r.add_post("/api/cflow/archive", h_cflow_archive)
    r.add_post("/api/cflow/approve", h_cflow_approve)
    r.add_post("/api/cflow/select", h_cflow_select)
    r.add_post("/api/cflow/nudge", h_cflow_nudge)
    r.add_post("/api/cflow/goto", h_cflow_goto)
    r.add_get("/api/mesh", h_mesh_list)
    r.add_post("/api/mesh", h_mesh_create)
    r.add_get("/api/mesh/{mesh}", h_mesh_get)
    r.add_delete("/api/mesh/{mesh}", h_mesh_delete)
    r.add_post("/api/mesh/{mesh}/members", h_mesh_join)
    r.add_delete("/api/mesh/{mesh}/members/{handle}", h_mesh_leave)
    r.add_post("/api/mesh/{mesh}/messages", h_mesh_send)
    r.add_get("/api/mesh/{mesh}/messages", h_mesh_history)
    r.add_post("/api/mesh/{mesh}/invite", h_mesh_invite)
    r.add_post("/api/mesh/link", h_mesh_link)
    r.add_get("/api/mesh/{mesh}/policy", h_mesh_policy_get)
    r.add_put("/api/mesh/{mesh}/policy", h_mesh_policy_set)
    # Peer federation endpoints. Deliberately outside /api/: the auth
    # middleware only guards /api/*, and these are called by *other daemons*
    # (via the relay's backend bridge) that hold mesh-scoped link tokens,
    # not this daemon's Bearer token. Each handler authenticates the caller
    # itself (invite consumption or the per-link token_in).
    r.add_post("/peer/mesh/link", h_peer_link)
    r.add_post("/peer/mesh/messages", h_peer_messages)
    r.add_post("/peer/mesh/members", h_peer_members)
    r.add_get("/api/sessions", h_sessions_list)
    r.add_post("/api/sessions", h_sessions_create)
    r.add_delete("/api/sessions", h_sessions_clear)
    r.add_get("/api/sessions/{name}", h_session_get)
    r.add_delete("/api/sessions/{name}", h_session_delete)
    r.add_post("/api/sessions/{name}/respawn", h_session_respawn)
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
    sessions = manager.list()
    return web.json_response(
        {
            "version": __version__,
            "uptime": round(time.monotonic() - request.app["started_at"], 1),
            # 'sessions' counts records, most of which may be exited ones kept
            # for respawn; 'running' is how many have a live child.
            "sessions": len(sessions),
            "running": sum(1 for s in sessions if not s.exited),
            "relay": request.app["relay_state"](),
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


def _scope_sessions(manager: SessionManager, cwd: str, scope: str) -> list:
    """The session this run maps 1:1 to (scope == session name), if alive."""
    for session in manager.list():
        if (
            session.sdef.name == scope
            and not session.exited
            and str(Path(session.sdef.cwd).resolve()) == cwd
        ):
            return [scope]
    return []


async def h_cflow_runs(request: web.Request) -> web.Response:
    """All monitorable cflow runs, keyed by (directory, scope): the
    machine-local run registry, plus every scope with state in an explicit
    ``?cwd=`` (reported even when idle). A run's ``scope`` is the managed
    session it belongs to (``default`` = started outside any session).
    """
    manager: SessionManager = request.app["manager"]
    keys: list = []
    for cwd, scope in cflow_state.known_runs():
        if (cwd, scope) not in keys:
            keys.append((cwd, scope))
    explicit = request.query.get("cwd")
    if explicit:
        explicit = str(Path(explicit).resolve())
        scopes = cflow_state.scopes_in(explicit) or [cflow_state.DEFAULT_SCOPE]
        wanted = request.query.get("scope")
        for scope in [wanted] if wanted else scopes:
            if (explicit, scope) not in keys:
                keys.append((explicit, scope))

    runs = []
    for cwd, scope in keys:
        entry = {
            "cwd": cwd,
            "scope": scope,
            "sessions": _scope_sessions(manager, cwd, scope),
        }
        try:
            payload = cflow_engine.status(cwd, scope=scope)
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
            for e in cflow_state.read_journal(cwd, scope, run_id=payload.get("run"))
            if e.get("event") == "step_report"
        ]
        runs.append({**entry, **payload, "reports": reports[-_CFLOW_REPORT_TAIL:]})
    return web.json_response({"runs": runs})


def _serialize_workflow(wf) -> dict:
    steps = []
    for s in wf.steps.values():
        entry = {
            "id": s.id,
            "title": s.title,
            "instructions": s.instructions,
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
    scope = request.query.get("scope") or cflow_state.DEFAULT_SCOPE
    manager: SessionManager = request.app["manager"]
    sessions = _scope_sessions(manager, cwd, scope)
    payload = cflow_engine.status(cwd, scope=scope)
    if payload.get("status") == "idle":
        return web.json_response(
            {"cwd": cwd, "scope": scope, "status": "idle", "sessions": sessions}
        )
    workflow = _serialize_workflow(cflow_state.load_snapshot(cwd, scope))
    journal = cflow_state.read_journal(cwd, scope, run_id=payload.get("run"))
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
            "scope": scope,
            "sessions": sessions,
            "run": payload,
            "workflow": workflow,
            "reports": reports,
            "journal": journal[-200:],
            # exactly what the manual Nudge button would type — shown to the
            # user for confirmation before sending
            "nudge_message": cflow_engine.NUDGE_CONTINUE,
        }
    )


async def _cflow_action_cwd(request: web.Request):
    body = await _json_body(request)
    raw = str(body.get("cwd") or "")
    if not raw:
        return None, json_error(400, "'cwd' required in the JSON body")
    scope = str(body.get("scope") or "") or cflow_state.DEFAULT_SCOPE
    return (str(Path(raw).resolve()), scope, body), None


async def _nudge_sessions(
    manager: SessionManager, cwd: str, scope: str, message: str
) -> list:
    """Type a resume nudge into the run's own session (scope == session
    name, 1:1), so an agent that stopped its turn picks the run back up.
    Default-scope runs belong to no session — nothing to nudge."""
    nudged = []
    for name in _scope_sessions(manager, cwd, scope):
        try:
            await manager.get(name).send_keys([message, "Enter"])
        except Exception:
            continue
        nudged.append(name)
    return nudged


async def h_cflow_workflows(request: web.Request) -> web.Response:
    """Workflows startable in a directory (project + global) — feeds the
    dashboard's start picker."""
    raw = request.query.get("cwd")
    if not raw:
        return json_error(400, "'cwd' query parameter required")
    cwd = str(Path(raw).resolve())
    flows = []
    for name, path in cflow_state.list_workflows(cwd):
        entry = {"name": name, "path": str(path)}
        try:
            wf = cflow_model.load(path)
            entry["description"] = wf.description
            entry["steps"] = wf.step_count()
        except WorkflowError as exc:
            entry["error"] = str(exc)
        flows.append(entry)
    return web.json_response({"workflows": flows})


async def h_cflow_start(request: web.Request) -> web.Response:
    """Start a run from the dashboard, then nudge the scope's session so its
    agent picks the run up. 400 while a run is still active in (cwd, scope) —
    archive it first; the web deliberately has no force path."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    workflow = str(body.get("workflow") or "")
    if not workflow:
        return json_error(400, "'workflow' required in the JSON body")
    context = str(body.get("context") or "") or None
    payload = cflow_engine.start(workflow, context=context, cwd=cwd, scope=scope)
    payload["nudged_sessions"] = await _nudge_sessions(
        request.app["manager"], cwd, scope, cflow_engine.NUDGE_STARTED
    )
    return web.json_response(payload)


async def h_cflow_archive(request: web.Request) -> web.Response:
    """Retire the run (finished or not) into the scope's archive folder,
    freeing the slot for a new start. An active run is aborted first."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, _ = resolved
    payload = cflow_engine.archive(by="web", cwd=cwd, scope=scope)
    return web.json_response(payload)


async def h_cflow_approve(request: web.Request) -> web.Response:
    """Approve the current gate / extend the loop limit — a human acting
    through the authenticated dashboard, same trust channel as the CLI.
    (Deliberately still not reachable by the agent: the MCP surface has no
    approve, and agents have no dashboard token.)"""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, _ = resolved
    payload = cflow_engine.approve(by="web", cwd=cwd, scope=scope)
    if payload.get("status") == "approved":
        payload["nudged_sessions"] = await _nudge_sessions(
            request.app["manager"], cwd, scope, cflow_engine.NUDGE_APPROVED
        )
    return web.json_response(payload)


async def h_cflow_select(request: web.Request) -> web.Response:
    """Confirm (or override) a user-chooser branch from the dashboard."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    option = str(body.get("option") or "")
    if not option:
        return json_error(400, "'option' required in the JSON body")
    reason = str(body.get("reason") or "") or None
    payload = cflow_engine.select(option, reason, by="web", cwd=cwd, scope=scope)
    if payload.get("status") == "selected":
        payload["nudged_sessions"] = await _nudge_sessions(
            request.app["manager"], cwd, scope, cflow_engine.NUDGE_SELECTED
        )
    return web.json_response(payload)


async def h_cflow_nudge(request: web.Request) -> web.Response:
    """Manually (re-)nudge the run directory's sessions from the dashboard —
    for when an auto-nudge was missed, or the agent simply stalled."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, _ = resolved
    nudged = await _nudge_sessions(
        request.app["manager"], cwd, scope, cflow_engine.NUDGE_CONTINUE
    )
    return web.json_response({"ok": True, "nudged_sessions": nudged})


async def h_cflow_goto(request: web.Request) -> web.Response:
    """Force the run's current step (human override), then nudge the
    directory's sessions so the agent continues from the new position."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    step = str(body.get("step") or "")
    if not step:
        return json_error(400, "'step' required in the JSON body")
    reason = str(body.get("reason") or "") or None
    payload = cflow_engine.goto(step, by="web", reason=reason, cwd=cwd, scope=scope)
    payload["nudged_sessions"] = await _nudge_sessions(
        request.app["manager"], cwd, scope, cflow_engine.nudge_for_state(step)
    )
    return web.json_response(payload)


# --------------------------------------------------------------------------- #
# mesh
# --------------------------------------------------------------------------- #
def _mesh_mgr(request: web.Request) -> MeshManager:
    return request.app["mesh"]


async def h_mesh_list(request: web.Request) -> web.Response:
    mm = _mesh_mgr(request)
    return web.json_response(
        {
            "meshes": [mm.mesh_info(m) for m in mm.list()],
            "relay": request.app["relay_state"](),
        }
    )


async def h_mesh_create(request: web.Request) -> web.Response:
    body = await _json_body(request)
    mesh = _mesh_mgr(request).create(str(body.get("name") or ""))
    return web.json_response(_mesh_mgr(request).mesh_info(mesh), status=201)


async def h_mesh_get(request: web.Request) -> web.Response:
    mm = _mesh_mgr(request)
    mesh = mm.get(request.match_info["mesh"])
    return web.json_response(
        {**mm.mesh_info(mesh), "relay": request.app["relay_state"]()}
    )


async def h_mesh_delete(request: web.Request) -> web.Response:
    _mesh_mgr(request).delete(request.match_info["mesh"])
    return web.json_response({"ok": True})


async def h_mesh_join(request: web.Request) -> web.Response:
    body = await _json_body(request)
    session = str(body.get("session") or "")
    if not session:
        return json_error(400, "'session' required in the JSON body")
    member = _mesh_mgr(request).join(
        request.match_info["mesh"],
        session,
        handle=str(body.get("handle") or ""),
        role=str(body.get("role") or ""),
    )
    return web.json_response(member.to_dict(), status=201)


async def h_mesh_leave(request: web.Request) -> web.Response:
    member = _mesh_mgr(request).leave(
        request.match_info["mesh"], request.match_info["handle"]
    )
    return web.json_response({"ok": True, "handle": member.handle})


async def h_mesh_send(request: web.Request) -> web.Response:
    body = await _json_body(request)
    sender = str(body.get("from") or "")
    to = body.get("to")
    text = body.get("body")
    if not sender:
        return json_error(400, "'from' required (a handle or a session name)")
    if not isinstance(to, (str, list)) or not to:
        return json_error(400, "'to' must be '*', a handle, or a list of handles")
    if not isinstance(text, str):
        return json_error(400, "'body' must be a string")
    sections = body.get("sections")
    result = _mesh_mgr(request).send(
        request.match_info["mesh"],
        sender,
        to,
        text,
        external=bool(body.get("external")),
        type=str(body.get("type") or "say"),
        reply_to=str(body.get("reply_to") or "") or None,
        sections=sections if isinstance(sections, dict) else None,
    )
    return web.json_response({**result, "relay": request.app["relay_state"]()})


async def h_mesh_history(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", 50))
    except ValueError:
        return json_error(400, "limit must be an integer")
    messages = _mesh_mgr(request).history(request.match_info["mesh"], limit)
    return web.json_response({"messages": messages})


async def h_mesh_policy_get(request: web.Request) -> web.Response:
    mm = _mesh_mgr(request)
    mesh = mm.get(request.match_info["mesh"])
    return web.json_response({"policy": mesh.policy})


async def h_mesh_policy_set(request: web.Request) -> web.Response:
    body = await _json_body(request)
    policy = _mesh_mgr(request).set_policy(request.match_info["mesh"], body)
    return web.json_response({"policy": policy})


async def h_mesh_invite(request: web.Request) -> web.Response:
    result = _mesh_mgr(request).invite(request.match_info["mesh"])
    return web.json_response({**result, "relay": request.app["relay_state"]()})


async def h_mesh_link(request: web.Request) -> web.Response:
    body = await _json_body(request)
    code = str(body.get("code") or "")
    if not code:
        return json_error(400, "'code' required in the JSON body")
    result = await _mesh_mgr(request).link(code)
    return web.json_response({**result, "relay": request.app["relay_state"]()})


async def h_peer_link(request: web.Request) -> web.Response:
    body = await _json_body(request)
    members = body.get("members")
    result = _mesh_mgr(request).peer_link_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        str(body.get("reply_token") or ""),
        members if isinstance(members, list) else [],
    )
    return web.json_response(result)


async def h_peer_messages(request: web.Request) -> web.Response:
    body = await _json_body(request)
    messages = body.get("messages")
    result = _mesh_mgr(request).peer_messages_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        messages if isinstance(messages, list) else [],
    )
    return web.json_response(result)


async def h_peer_members(request: web.Request) -> web.Response:
    body = await _json_body(request)
    members = body.get("members")
    result = _mesh_mgr(request).peer_members_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        members if isinstance(members, list) else [],
    )
    return web.json_response(result)


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


async def h_sessions_clear(request: web.Request) -> web.Response:
    """Drop the records of all exited sessions (``?logs=1`` also deletes their
    captured output). Running sessions are untouched.

    The daemon keeps exited sessions around indefinitely so they stay
    respawnable, so this is the explicit cleanup — nothing else discards them
    in bulk.
    """
    manager: SessionManager = request.app["manager"]
    logs = request.query.get("logs") in ("1", "true")
    removed = manager.clear(logs=logs)
    return web.json_response({"removed": removed, "logs": logs})


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


async def h_session_respawn(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    session = manager.respawn(request.match_info["name"])
    return web.json_response(session.info())


async def h_session_keys(request: web.Request) -> web.Response:
    session = _session(request)
    body = await _json_body(request)
    paste = body.get("paste")
    if paste is not None:
        if not isinstance(paste, str):
            return json_error(400, "'paste' must be a string")
        data = await session.paste(paste, enter=bool(body.get("enter")))
        return web.json_response({"ok": True, "bytes": len(data)})
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
