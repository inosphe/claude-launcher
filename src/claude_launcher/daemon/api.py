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

from .. import __version__, harnesses as harness_registry
from .. import profile as profile_mod, spawn as spawn_mod, workspaces
from ..cflow import engine as cflow_engine, model as cflow_model, state as cflow_state
from ..cflow.engine import CflowError
from ..cflow.model import WorkflowError
from ..cflow.state import LockBusy, StateError
from ..profile import ProfileError
from . import mesh_roles
from .harness import HarnessError, SessionDef
from .manager import ManagerError, SessionManager
from .mesh import MeshConflict, MeshError, MeshManager
from .session import STATUS_IDLE, SessionGone
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
async def revalidate_middleware(request: web.Request, handler):
    """Make the dashboard's own assets always revalidate.

    ``index.html`` names ``static/app.js`` with no version, and aiohttp's
    static handler sends no ``Cache-Control`` — so browsers fall back to
    *heuristic* freshness (a fraction of the file's age) and serve a stale
    bundle without ever asking us. A daemon that has already been upgraded
    then keeps rendering the old UI, which reads as "the fix did not ship".

    ``no-cache`` does not mean "do not store": the ETag and Last-Modified
    the static handler already sends turn each load into a conditional GET
    that answers 304 in a couple of hundred bytes. Correctness for the cost
    of one round-trip per asset.
    """
    response = await handler(request)
    path = request.path
    if path == "/" or path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
    return response


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except (SessionGone, MeshConflict, LockBusy) as exc:
        # LockBusy is transient by construction (the other writer is mid-
        # transition), so it gets a retryable status, not a flat 400.
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
        middlewares=[
            revalidate_middleware,
            error_middleware,
            build_auth_middleware(token, cookie_sessions),
        ]
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
    r.add_get("/api/roles", h_roles)
    r.add_get("/api/workspaces", h_workspaces)
    r.add_post("/api/workspaces", h_workspace_add)
    r.add_delete("/api/workspaces/{name}", h_workspace_remove)
    r.add_get("/api/harnesses", h_harnesses)
    r.add_get("/api/cflow", h_cflow_runs)
    r.add_get("/api/cflow/run", h_cflow_run_detail)
    r.add_get("/api/cflow/workflows", h_cflow_workflows)
    r.add_post("/api/cflow/start", h_cflow_start)
    r.add_post("/api/cflow/request", h_cflow_request)
    r.add_post("/api/cflow/request/cancel", h_cflow_request_cancel)
    r.add_post("/api/cflow/archive", h_cflow_archive)
    r.add_post("/api/cflow/approve", h_cflow_approve)
    r.add_post("/api/cflow/select", h_cflow_select)
    r.add_post("/api/cflow/nudge", h_cflow_nudge)
    r.add_post("/api/cflow/goto", h_cflow_goto)
    r.add_get("/api/mesh", h_mesh_list)
    r.add_post("/api/mesh", h_mesh_create)
    r.add_delete("/api/mesh/outgoing/{rid}", h_mesh_outgoing_cancel)
    r.add_get("/api/mesh/{mesh}", h_mesh_get)
    r.add_delete("/api/mesh/{mesh}", h_mesh_delete)
    r.add_post("/api/mesh/{mesh}/members", h_mesh_join)
    r.add_delete("/api/mesh/{mesh}/members/{handle}", h_mesh_leave)
    r.add_post("/api/mesh/{mesh}/messages", h_mesh_send)
    r.add_get("/api/mesh/{mesh}/messages", h_mesh_history)
    r.add_get("/api/mesh/{mesh}/owed", h_mesh_owed)
    r.add_post("/api/mesh/{mesh}/invite", h_mesh_invite)
    r.add_get("/api/mesh/{mesh}/invites", h_mesh_invites_list)
    r.add_delete("/api/mesh/{mesh}/invites/{prefix}", h_mesh_invite_revoke)
    r.add_post("/api/mesh/{mesh}/requests/{rid}/approve", h_mesh_request_approve)
    r.add_post("/api/mesh/{mesh}/requests/{rid}/deny", h_mesh_request_deny)
    r.add_delete("/api/mesh/{mesh}/guests/{machine}", h_mesh_guest_revoke)
    r.add_put("/api/mesh/{mesh}/peers", h_mesh_peers_reorder)
    r.add_patch("/api/mesh/{mesh}/links/{a}/{b}", h_mesh_link_set)
    r.add_patch("/api/mesh/{mesh}/members/{a}/links/{b}", h_mesh_member_link_set)
    r.add_get("/api/mesh/{mesh}/policy", h_mesh_policy_get)
    r.add_put("/api/mesh/{mesh}/policy", h_mesh_policy_set)
    r.add_get("/api/mesh/{mesh}/roles", h_mesh_roles_get)
    r.add_put("/api/mesh/{mesh}/roles", h_mesh_roles_set)
    r.add_post("/api/mesh/{mesh}/invitations", h_mesh_invitation)
    r.add_get("/api/relay/peers", h_relay_peers)
    r.add_get("/api/relay/peers/{machine}/sessions", h_relay_peer_sessions)
    # Peer federation endpoints. Deliberately outside /api/: the auth
    # middleware only guards /api/*, and these are called by *other daemons*
    # (via the relay's backend bridge) that hold mesh-scoped link tokens,
    # not this daemon's Bearer token. Each handler authenticates the caller
    # itself (invite consumption or the per-link token_in).
    r.add_post("/peer/mesh/join_request", h_peer_join_request)
    r.add_post("/peer/mesh/grant", h_peer_grant)
    r.add_post("/peer/mesh/invite", h_peer_mesh_invite)
    r.add_post("/peer/mesh/unlink", h_peer_unlink)
    # Same-relay convenience surface (one relay = one operator's machines):
    # lets a mesh owner's wizard enumerate a peer daemon's sessions before
    # pushing an invitation. Session names only — no capture, no control.
    r.add_post("/peer/sessions", h_peer_sessions)
    r.add_post("/peer/mesh/join", h_peer_join)
    r.add_post("/peer/mesh/leave", h_peer_leave)
    r.add_post("/peer/mesh/link", h_peer_link)
    r.add_post("/peer/mesh/member-link", h_peer_member_link)
    r.add_post("/peer/mesh/roles", h_peer_roles)
    r.add_post("/peer/mesh/send", h_peer_send)
    r.add_post("/peer/mesh/sync", h_peer_sync)
    r.add_post("/peer/mesh/deliver", h_peer_deliver)
    r.add_get("/api/sessions", h_sessions_list)
    r.add_post("/api/sessions", h_sessions_create)
    r.add_delete("/api/sessions", h_sessions_clear)
    r.add_get("/api/sessions/{name}", h_session_get)
    r.add_get("/api/sessions/{name}/meta", h_session_meta)
    r.add_get("/api/sessions/{name}/children", h_session_children)
    r.add_post("/api/sessions/{name}/children", h_session_spawn)
    r.add_delete("/api/sessions/{name}", h_session_delete)
    r.add_post("/api/sessions/{name}/respawn", h_session_respawn)
    r.add_post("/api/sessions/{name}/keys", h_session_keys)
    r.add_post("/api/sessions/{name}/deliver", h_session_deliver)
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


async def h_harnesses(request: web.Request) -> web.Response:
    """The declared harnesses, each with whether this machine can run it.

    ``available`` is reported rather than filtered on: a harness claunch knows
    about but the machine has not installed is a *different* thing from one
    claunch does not know about, and the picker should say which it is.
    """
    return web.json_response(
        {
            "harnesses": [
                harness_registry.registry()[name].to_dict()
                for name in harness_registry.names()
            ]
        }
    )


async def h_workspaces(request: web.Request) -> web.Response:
    """The directories a session may be spawned in, for the pickers."""
    return web.json_response(
        {"workspaces": [w.to_dict() for w in workspaces.list_all()]}
    )


async def h_workspace_add(request: web.Request) -> web.Response:
    """Register a directory — the browser's ``claunch workspace add``.

    Writable, where the create form's directory field deliberately is not, and
    the difference is not a contradiction but the whole shape of the feature:
    a free-text path is typed **once**, here, where it is checked against the
    filesystem before it is stored and the answer comes back immediately. What
    the registry removes is that same path being retyped at every spawn, where
    a typo surfaces late and blames the harness. Registering is the vouching
    step; it cannot happen without someone spelling a directory out.

    The path is resolved on the **daemon**, which is what a workspace means —
    a browser on another machine is describing the daemon's filesystem, not
    its own.
    """
    body = await _json_body(request)
    try:
        workspace = workspaces.add(
            str(body.get("path") or ""), str(body.get("name") or "") or None
        )
    except workspaces.WorkspaceError as exc:
        return json_error(400, str(exc))
    return web.json_response({"workspace": workspace.to_dict()}, status=201)


async def h_workspace_remove(request: web.Request) -> web.Response:
    """Unregister one workspace. The directory itself is never touched.

    Sessions already running in it are left alone too: their cwd was resolved
    when they spawned, so unregistering decides what may be spawned *next*,
    not what is running now.
    """
    try:
        removed = workspaces.remove(request.match_info["name"])
    except workspaces.WorkspaceError as exc:
        return json_error(404, str(exc))
    return web.json_response({"workspace": removed.to_dict()})


async def h_roles(request: web.Request) -> web.Response:
    """The roles a session can be spawned with — the packaged vocabulary.

    Deliberately not a mesh's role set: a session being spawned belongs to no
    mesh yet, and a per-mesh override is scoped to that mesh's roster (see
    :mod:`mesh_roles`). The stance travels with each entry so the picker can
    show what a role would inject before anyone commits to it.
    """
    roleset = mesh_roles.resolve()
    return web.json_response(
        {
            "roles": [
                {
                    "name": r.name,
                    "aliases": list(r.aliases),
                    "stance": r.stance,
                    "prompt": mesh_roles.system_prompt(r),
                }
                for r in (roleset.roles[n] for n in sorted(roleset.roles))
            ]
        }
    )


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
        entry = _cflow_entry(manager, cwd, scope)
        # An idle slot is only interesting when it was asked about explicitly,
        # or when a human's start request is waiting to be picked up there.
        if (
            entry.get("status") == "idle"
            and cwd != explicit
            and not entry.get("pending_start")
        ):
            continue
        runs.append(entry)
    return web.json_response({"runs": runs})


def _cflow_entry(manager: SessionManager, cwd: str, scope: str) -> dict:
    """One (cwd, scope) slot as the dashboard sees it: live status, the recent
    step reports, and any pending start request."""
    entry = {
        "cwd": cwd,
        "scope": scope,
        "sessions": _scope_sessions(manager, cwd, scope),
    }
    try:
        payload = cflow_engine.status(cwd, scope=scope)
    except (CflowError, WorkflowError, StateError, OSError) as exc:
        return {**entry, "status": "error", "error": str(exc)}
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
    return {**entry, **payload, "reports": reports[-_CFLOW_REPORT_TAIL:]}


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
            {
                "cwd": cwd,
                "scope": scope,
                "status": "idle",
                "sessions": sessions,
                "pending_start": payload.get("pending_start"),
            }
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
            "pending_start": payload.get("pending_start"),
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
    cwd = Path(raw).resolve()
    # Checked before anything touches the slot: acting on a run in a directory
    # that is not there is always a mistake, and the first thing a mutating
    # action does is take the slot's lock — which would otherwise create
    # `<typo>/.cflow/runs/<scope>/` on the way to failing.
    if not cwd.is_dir():
        return None, json_error(400, f"no such directory: {cwd}")
    scope = str(body.get("scope") or "") or cflow_state.DEFAULT_SCOPE
    return (str(cwd), scope, body), None


async def _nudge_sessions(
    manager: SessionManager, cwd: str, scope: str, message: str
) -> list:
    """Type a resume nudge into the run's own session (scope == session
    name, 1:1), so an agent that stopped its turn picks the run back up.
    Default-scope runs belong to no session — nothing to nudge."""
    nudged = []
    for name in _scope_sessions(manager, cwd, scope):
        try:
            session = manager.get(name)
        except Exception:  # noqa: BLE001 — raced with a removal
            continue
        if await session.deliver(message):
            nudged.append(name)
    return nudged


def _startable_workflows(cwd: str) -> list:
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
    return flows


async def h_cflow_workflows(request: web.Request) -> web.Response:
    """Workflows startable in a directory (project + global) — feeds the
    dashboard's start picker."""
    raw = request.query.get("cwd")
    if not raw:
        return json_error(400, "'cwd' query parameter required")
    cwd = str(Path(raw).resolve())
    return web.json_response({"workflows": _startable_workflows(cwd)})


async def h_cflow_request(request: web.Request) -> web.Response:
    """Ask the scope's agent to start a workflow, and nudge it to look.

    The preferred of the two creation paths (see ``h_cflow_start`` for the
    other): nothing is written except the request itself, the agent performs
    the ``start``, and so the run on disk and the run the agent believes it is
    driving are the same object by construction.
    """
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    workflow = str(body.get("workflow") or "")
    if not workflow:
        return json_error(400, "'workflow' required in the JSON body")
    context = str(body.get("context") or "") or None
    payload = cflow_engine.request_start(
        workflow, context=context, by="web", cwd=cwd, scope=scope
    )
    name = (payload.get("request") or {}).get("workflow") or workflow
    payload["nudged_sessions"] = await _nudge_sessions(
        request.app["manager"], cwd, scope, cflow_engine.nudge_for_request(name)
    )
    return web.json_response(payload)


async def h_cflow_request_cancel(request: web.Request) -> web.Response:
    """Withdraw a pending start request (only until the agent acts on it)."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, _ = resolved
    payload = cflow_engine.cancel_request(by="web", cwd=cwd, scope=scope)
    return web.json_response(payload)


async def h_cflow_start(request: web.Request) -> web.Response:
    """Start a run *directly* from the dashboard, then nudge the scope's
    session so its agent picks the run up. 400 while a run is still active in
    (cwd, scope) — archive it first; the web deliberately has no force path.

    This writes a run the agent has not read, which is why the dashboard
    offers it as the fallback: for a scope with no live session (an agent that
    will attach later, an orchestrator script) and for a human who explicitly
    wants the run to exist now. When a session *is* live, prefer
    ``/api/cflow/request``.
    """
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
            "outgoing": mm.outgoing_list(),
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
    result = await _mesh_mgr(request).join(
        request.match_info["mesh"],
        session,
        handle=str(body.get("handle") or ""),
        role=str(body.get("role") or ""),
        code=str(body.get("code") or "") or None,
    )
    if isinstance(result, dict):  # codeless remote join: pended for approval
        return web.json_response(result, status=202)
    return web.json_response(result.to_dict(), status=201)


async def h_mesh_leave(request: web.Request) -> web.Response:
    member = await _mesh_mgr(request).leave(
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
    result = await _mesh_mgr(request).send(
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


async def h_mesh_owed(request: web.Request) -> web.Response:
    """Who has been asked something and answered nothing — per message."""
    mm = _mesh_mgr(request)
    return web.json_response(mm.owed_report(mm.get(request.match_info["mesh"])))


async def h_mesh_policy_get(request: web.Request) -> web.Response:
    mm = _mesh_mgr(request)
    mesh = mm.get(request.match_info["mesh"])
    return web.json_response({"policy": mesh.policy})


async def h_mesh_policy_set(request: web.Request) -> web.Response:
    body = await _json_body(request)
    policy = _mesh_mgr(request).set_policy(request.match_info["mesh"], body)
    return web.json_response({"policy": policy})


async def h_mesh_roles_get(request: web.Request) -> web.Response:
    return web.json_response(
        _mesh_mgr(request).roles_view(request.match_info["mesh"])
    )


async def h_mesh_roles_set(request: web.Request) -> web.Response:
    """Upload this mesh's role set, or reset it to the packaged vocabulary.

    The body is ``{"yaml": "..."}`` (what a user edits) or ``{"roles": {...}}``
    (an already-parsed document); either may be null to reset. Uploads are not
    retroactive — members already on the roster keep the role they joined with.
    """
    body = await _json_body(request)
    if "yaml" in body:
        doc = body.get("yaml")
        if doc is not None and not isinstance(doc, str):
            return json_error(400, "'yaml' must be a string or null")
        if isinstance(doc, str) and not doc.strip():
            doc = None  # an emptied editor means "reset", not "empty set"
    elif "roles" in body:
        doc = body.get("roles")
    else:
        return json_error(400, "send {'yaml': ...} or {'roles': ...}")
    result = await _mesh_mgr(request).set_roles(request.match_info["mesh"], doc)
    return web.json_response(result)


async def h_mesh_invite(request: web.Request) -> web.Response:
    result = _mesh_mgr(request).invite(request.match_info["mesh"])
    return web.json_response({**result, "relay": request.app["relay_state"]()})


async def h_mesh_invites_list(request: web.Request) -> web.Response:
    return web.json_response(
        {"invites": _mesh_mgr(request).invite_list(request.match_info["mesh"])}
    )


async def h_mesh_invite_revoke(request: web.Request) -> web.Response:
    revoked = _mesh_mgr(request).invite_revoke(
        request.match_info["mesh"], request.match_info["prefix"]
    )
    return web.json_response({"revoked": revoked})


async def h_mesh_request_approve(request: web.Request) -> web.Response:
    result = await _mesh_mgr(request).approve_request(
        request.match_info["mesh"], request.match_info["rid"]
    )
    return web.json_response(result)


async def h_mesh_request_deny(request: web.Request) -> web.Response:
    result = await _mesh_mgr(request).deny_request(
        request.match_info["mesh"], request.match_info["rid"]
    )
    return web.json_response(result)


async def h_mesh_invitation(request: web.Request) -> web.Response:
    body = await _json_body(request)
    machine = str(body.get("machine") or "")
    session = str(body.get("session") or "")
    if not machine or not session:
        return json_error(400, "'machine' and 'session' required")
    member = await _mesh_mgr(request).invite_member(
        request.match_info["mesh"],
        machine,
        session,
        handle=str(body.get("handle") or ""),
        role=str(body.get("role") or ""),
    )
    return web.json_response({"member": member}, status=201)


async def h_relay_peers(request: web.Request) -> web.Response:
    mm = _mesh_mgr(request)
    if mm.peer_lister is None:
        return json_error(
            400, "relay uplink is not running — no peers to list"
        )
    try:
        names = await mm.peer_lister()
    except Exception as exc:  # noqa: BLE001 — surface PeerError as 400
        return json_error(400, str(exc))
    return web.json_response(
        {"peers": sorted(names), "relay": request.app["relay_state"]()}
    )


async def h_relay_peer_sessions(request: web.Request) -> web.Response:
    mm = _mesh_mgr(request)
    if mm.peer_transport is None:
        return json_error(400, "relay uplink is not running")
    machine = request.match_info["machine"]
    payload = await mm.peer_transport(machine, "/peer/sessions", {})
    return web.json_response(
        {"machine": machine, "sessions": payload.get("sessions", [])}
    )


async def h_mesh_outgoing_cancel(request: web.Request) -> web.Response:
    result = _mesh_mgr(request).cancel_request(request.match_info["rid"])
    return web.json_response(result)


async def h_mesh_guest_revoke(request: web.Request) -> web.Response:
    result = await _mesh_mgr(request).revoke_guest(
        request.match_info["mesh"], request.match_info["machine"]
    )
    return web.json_response(result)


async def h_mesh_peers_reorder(request: web.Request) -> web.Response:
    """Rewrite the rank list — rank 0 is the mesh's authority."""
    body = await _json_body(request)
    order = body.get("order")
    if not isinstance(order, list):
        return json_error(400, "'order' must be a list of machine names")
    result = await _mesh_mgr(request).reorder_peers(
        request.match_info["mesh"],
        [str(m) for m in order],
        force=bool(body.get("force")),
    )
    return web.json_response(result)


async def h_mesh_link_set(request: web.Request) -> web.Response:
    """Cut or restore the direct edge between two peers."""
    body = await _json_body(request)
    if "enabled" not in body:
        return json_error(400, "'enabled' must be true or false")
    result = await _mesh_mgr(request).set_link(
        request.match_info["mesh"],
        request.match_info["a"],
        request.match_info["b"],
        enabled=bool(body.get("enabled")),
    )
    return web.json_response(result)


async def h_mesh_member_link_set(request: web.Request) -> web.Response:
    """Connect or disconnect two members — the mesh's own topology, one
    layer up from the peer-daemon graph ``/links`` edits.

    ``actor`` names the session asking, and is what makes this callable by an
    agent: with it, the edit is checked against the session tree (a session
    rewires only what it spawned). Without it the caller is a human at the
    CLI or dashboard, who owns the whole graph.
    """
    body = await _json_body(request)
    if "enabled" not in body:
        return json_error(400, "'enabled' must be true or false")
    result = await _mesh_mgr(request).set_member_link(
        request.match_info["mesh"],
        request.match_info["a"],
        request.match_info["b"],
        enabled=bool(body.get("enabled")),
        actor=str(body.get("actor") or ""),
    )
    return web.json_response(result)


async def h_peer_member_link(request: web.Request) -> web.Response:
    """A peer forwards a member-graph edit up to us (the authority)."""
    body = await _json_body(request)
    if "enabled" not in body:
        return json_error(400, "'enabled' must be true or false")
    result = _mesh_mgr(request).peer_member_link_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        str(body.get("a") or ""),
        str(body.get("b") or ""),
        bool(body.get("enabled")),
    )
    return web.json_response(result)


async def h_peer_join_request(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = _mesh_mgr(request).peer_join_request_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("session") or ""),
        str(body.get("handle") or ""),
        str(body.get("role") or ""),
        str(body.get("reply_token") or ""),
        str(body.get("code") or ""),
    )
    return web.json_response(result)


async def h_peer_grant(request: web.Request) -> web.Response:
    body = await _json_body(request)
    grant = body.get("grant")
    result = _mesh_mgr(request).peer_grant_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("request_id") or ""),
        str(body.get("token") or ""),
        bool(body.get("denied")),
        grant if isinstance(grant, dict) else None,
    )
    return web.json_response(result)


async def h_peer_mesh_invite(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = await _mesh_mgr(request).peer_invite_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("session") or ""),
        str(body.get("handle") or ""),
        str(body.get("role") or ""),
        str(body.get("code") or ""),
    )
    return web.json_response(result)


async def h_peer_sessions(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    return web.json_response(
        {
            "sessions": [
                {"name": s.sdef.name, "status": s.status()}
                for s in manager.list()
                if not s.exited
            ]
        }
    )


async def h_peer_unlink(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = _mesh_mgr(request).peer_unlink_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
    )
    return web.json_response(result)


async def h_peer_join(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = _mesh_mgr(request).peer_join_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        str(body.get("session") or ""),
        str(body.get("handle") or ""),
        str(body.get("role") or ""),
    )
    return web.json_response(result)


async def h_peer_leave(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = _mesh_mgr(request).peer_leave_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        str(body.get("handle") or ""),
    )
    return web.json_response(result)


async def h_peer_link(request: web.Request) -> web.Response:
    """A peer asks us to cut or restore an edge it terminates."""
    body = await _json_body(request)
    if "enabled" not in body:
        return json_error(400, "'enabled' must be true or false")
    result = _mesh_mgr(request).peer_link_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        str(body.get("a") or ""),
        str(body.get("b") or ""),
        bool(body.get("enabled")),
    )
    return web.json_response(result)


async def h_peer_roles(request: web.Request) -> web.Response:
    """A peer asks us, the authority, to change the mesh's role set."""
    body = await _json_body(request)
    result = _mesh_mgr(request).peer_roles_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        body.get("roles"),
    )
    return web.json_response(result)


async def h_peer_send(request: web.Request) -> web.Response:
    body = await _json_body(request)
    message = body.get("message")
    result = _mesh_mgr(request).peer_send_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        message if isinstance(message, dict) else {},
    )
    return web.json_response(result)


async def h_peer_sync(request: web.Request) -> web.Response:
    body = await _json_body(request)
    try:
        base = int(body.get("base") or 0)
    except (TypeError, ValueError):
        return json_error(400, "'base' must be an integer")
    messages = body.get("messages")
    members = body.get("members")
    nudges = body.get("nudges")
    peers = body.get("peers")
    links = body.get("links")
    result = _mesh_mgr(request).peer_sync_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        base,
        messages if isinstance(messages, list) else [],
        members if isinstance(members, list) else [],
        body.get("policy"),
        nudges if isinstance(nudges, list) else [],
        peers=peers if isinstance(peers, list) else None,
        epoch=body.get("epoch"),
        links=links if isinstance(links, list) else None,
        edges=body.get("edges") if isinstance(body.get("edges"), dict) else None,
        member_edges=(
            body.get("member_edges")
            if isinstance(body.get("member_edges"), dict) else None
        ),
        roles=body.get("roles") if isinstance(body.get("roles"), dict) else None,
    )
    return web.json_response(result)


async def h_peer_deliver(request: web.Request) -> web.Response:
    """Fast path: a peer delivers a send its authority has not sequenced."""
    body = await _json_body(request)
    message = body.get("message")
    result = _mesh_mgr(request).peer_deliver_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        message if isinstance(message, dict) else {},
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


async def h_session_children(request: web.Request) -> web.Response:
    """A session's subtree plus what it may still spawn."""
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    manager.get(name)  # 404 for an unknown parent, before reporting on it
    return web.json_response(
        {
            "session": name,
            "parent": manager.get(name).sdef.parent,
            "children": [
                {
                    "name": child,
                    "status": manager.get(child).status(),
                    "children": manager.children(child),
                }
                for child in manager.children(name)
            ],
            "descendants": manager.descendants(name),
            **manager.spawn_capabilities(name),
        }
    )


async def h_session_spawn(request: web.Request) -> web.Response:
    """Create a child session — the agent-facing way a session grows a team.

    One call because the steps are not independently useful: a child spawned
    but not enrolled is a terminal nobody is listening to, and a child
    enrolled but not briefed is an agent that does not know why it exists.
    Doing them here also makes the ordering a property of the daemon rather
    than of whichever client got it right — join before the opening task, so
    the child's first turn already has its mesh identity.

    Everything past the session itself is optional and reported back
    individually, so a partial success is legible: the caller is told the
    child exists even when the mesh join is what failed.
    """
    manager: SessionManager = request.app["manager"]
    parent = request.match_info["name"]
    body = await _json_body(request)
    try:
        session = manager.spawn(parent, body)
    except spawn_mod.SpawnDenied as exc:
        return json_error(403, str(exc))
    except (HarnessError, ValueError, TypeError) as exc:
        return json_error(400, f"bad spawn request: {exc}")
    except ManagerError as exc:
        return json_error(409 if "already exists" in str(exc) else 400, str(exc))

    result = {"session": session.info(), "parent": parent}
    mesh_name = str(body.get("mesh") or "").strip()
    if mesh_name:
        result["mesh"] = await _spawn_join_mesh(request, parent, session, body)
    workflow = str(body.get("workflow") or "").strip()
    if workflow:
        result["workflow"] = _spawn_start_workflow(session, workflow, body)
    task = str(body.get("task") or "").strip()
    if task:
        result["task"] = await _spawn_open_task(session, task)
    return web.json_response(result, status=201)


async def _spawn_join_mesh(
    request: web.Request, parent: str, session, body: dict
) -> dict:
    """Enrol the new child, then cut it down to the peers it should see.

    The child starts connected to its parent only. That is the conservative
    direction: a child that cannot yet reach a peer says so and asks, while a
    child wired to everyone by default has already broadcast to them by the
    time anyone notices the arrangement was wrong.
    """
    mm = _mesh_mgr(request)
    mesh_name = str(body.get("mesh") or "").strip()
    manager: SessionManager = request.app["manager"]
    try:
        member = await mm.join(
            mesh_name,
            session.sdef.name,
            handle=str(body.get("handle") or ""),
            role=str(body.get("role") or ""),
        )
    except MeshError as exc:
        return {"ok": False, "error": str(exc)}
    if isinstance(member, dict):  # a remote join pended for approval
        return {"ok": False, "pending": member}

    # The parent's own handle in this mesh, if it has one — the single peer
    # the child keeps. A parent that is not itself a member leaves the child
    # connected to whatever `connect` names, or to nobody.
    parent_member = mm.resolve_sender(mesh_name, parent)
    keep = {str(h) for h in (body.get("connect") or []) if str(h).strip()}
    if parent_member is not None:
        keep.add(parent_member.handle)
    try:
        cut = await mm.isolate_member(mesh_name, member.handle, keep=keep)
    except MeshError as exc:
        return {"ok": True, "handle": member.handle, "isolate_error": str(exc)}
    del manager  # only needed for the lookup above
    return {
        "ok": True,
        "mesh": mesh_name,
        "handle": member.handle,
        "role": member.role,
        "connected_to": sorted(keep),
        "disconnected_from": cut,
    }


def _spawn_start_workflow(session, workflow: str, body: dict) -> dict:
    """Start a cflow run scoped to the child.

    Runs are keyed by ``(cwd, scope)`` and a session-scoped run uses the
    session name as its scope, so the child's own agent picks this up as
    *its* run — not the parent's, even though both sit in the same directory.
    """
    try:
        payload = cflow_engine.start(
            workflow,
            context=str(body.get("context") or "") or None,
            cwd=session.sdef.cwd,
            scope=session.sdef.name,
        )
    except Exception as exc:  # noqa: BLE001 — engine raises several types
        return {"ok": False, "error": str(exc)}
    return {
        "ok": True,
        "workflow": workflow,
        "scope": session.sdef.name,
        "step": payload.get("step") or payload.get("id"),
    }


async def _spawn_open_task(session, task: str) -> dict:
    """Type the child's opening instruction once it has finished booting.

    Fire-and-forget for the same reason the mesh briefing is: the harness
    takes seconds to come up, and a spawn call that waited for it would tie
    the parent's tool call to another program's startup time. The injection
    is idle-gated, so it lands in a prompt rather than halfway through one.
    """
    asyncio.ensure_future(_deliver_task(session, task))
    return {"ok": True, "queued": True}


#: How long the child must stay idle before its opening task is typed in.
#: A single idle *sample* is not enough: the mesh join briefing is pasted at
#: almost the same moment, and its submitting Enter is a deliberately delayed
#: write (``PASTE_ENTER_DELAY``), so there is a real window where the session
#: reads idle mid-briefing. Typing into that window glues the task onto the
#: briefing's closing fence. Comfortably longer than that delay, and longer
#: than the gap between a harness printing its banner and taking input.
TASK_SETTLE = 1.5


async def _deliver_task(session, task: str, *, hold: float = 60.0) -> None:
    deadline = time.monotonic() + hold
    idle_since = None
    while time.monotonic() < deadline:
        if session.exited:
            return
        if session.status() == STATUS_IDLE:
            if idle_since is None:
                idle_since = time.monotonic()
            elif time.monotonic() - idle_since >= TASK_SETTLE:
                break
        else:
            idle_since = None  # something is still arriving; start over
        await asyncio.sleep(0.2)
    else:
        return  # never settled; better nothing than a half-typed prompt
    await session.deliver(task)


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


async def h_session_meta(request: web.Request) -> web.Response:
    """Everything known *about* one session, gathered in one call.

    A session is described by four registries that otherwise only meet in the
    operator's head: its own definition (harness, profile, role, conversation),
    the workspace its directory belongs to, the meshes it is a member of, and —
    the reason this endpoint exists — the cflow run it drives. That last link
    is exact rather than heuristic: a run is keyed by (directory, scope) and
    the scope IS the session name, so a session maps to exactly one slot, and
    the workflows startable in it are the ones declared in its directory.
    """
    manager: SessionManager = request.app["manager"]
    session = manager.get(request.match_info["name"])
    info = session.info()
    name = info["name"]
    cwd = str(Path(info["cwd"]).resolve()) if info.get("cwd") else ""

    harness = harness_registry.registry().get(info.get("harness") or "")
    workspace = next(
        (w for w in workspaces.list_all() if _same_dir(w.path, cwd)), None
    )
    role = None
    if info.get("role"):
        roleset = mesh_roles.resolve()
        entry = roleset.roles.get(info["role"])
        if entry:
            role = {"name": entry.name, "stance": entry.stance}

    body = {
        "session": info,
        "harness": harness.to_dict() if harness else None,
        "workspace": workspace.to_dict() if workspace else None,
        "role": role,
        "meshes": request.app["mesh"].meshes_for_session(name),
        "cflow": None,
        "workflows": [],
    }
    if cwd:
        body["cflow"] = _cflow_entry(manager, cwd, name)
        body["workflows"] = _startable_workflows(cwd)
    return web.json_response(body)


def _same_dir(a: str, b: str) -> bool:
    if not a or not b:
        return False
    try:
        return Path(a).resolve() == Path(b).resolve()
    except OSError:
        return False


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


async def h_session_deliver(request: web.Request) -> web.Response:
    """Hand a message to the agent in this session — the out-of-process door
    to :meth:`Session.deliver`, for senders that live outside the daemon (the
    CLI's cflow nudges). ``/keys`` stays the raw keyboard passthrough."""
    session = _session(request)
    body = await _json_body(request)
    text = body.get("text")
    if not isinstance(text, str) or not text:
        return json_error(400, "'text' must be a non-empty string")
    delivered = await session.deliver(text)
    return web.json_response({"ok": True, "delivered": delivered})


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
