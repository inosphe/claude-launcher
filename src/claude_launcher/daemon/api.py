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
from typing import List, Optional

from aiohttp import web

from .. import __version__, harnesses as harness_registry
from .. import profile as profile_mod, spawn as spawn_mod, workspaces
from . import onboard
from ..cflow import engine as cflow_engine, model as cflow_model, state as cflow_state
from ..cflow.engine import CflowError
from ..cflow.model import WorkflowError
from ..cflow.state import LockBusy, StateError
from ..profile import ProfileError
from . import mesh_roles
from .harness import CLAUDE_HARNESS, HarnessError, SessionDef
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
    # Identifies this daemon *process*, and is handed out by /api/health (which
    # needs no auth), /api/daemon and every terminal socket's init frame. A
    # value a client has not seen before means the daemon it was talking to is
    # gone: its login cookie died with it (they live in memory, above), the pids
    # it published belong to the previous incarnation, and any socket still held
    # open is bound to nothing. Uptime could be read the same way, but only by
    # subtraction and only if the client kept the previous reading; an id says
    # it outright, and says it identically on all three surfaces.
    boot_id = secrets.token_hex(8)
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
    app["boot_id"] = boot_id
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
    r.add_get("/api/mesh/{mesh}/flows", h_mesh_flows)
    r.add_post("/api/mesh/{mesh}/members/{handle}/nudge", h_mesh_nudge)
    # Dismissing is deleting from the ledger, so it is a DELETE against it:
    # the whole of a member's unanswered mail, or one message of it.
    r.add_delete("/api/mesh/{mesh}/members/{handle}/owed", h_mesh_owed_dismiss)
    r.add_delete(
        "/api/mesh/{mesh}/members/{handle}/owed/{id}", h_mesh_owed_dismiss
    )
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
    # The bulk verbs, one segment deep so they cannot be read as a session
    # name: nothing else routes POST /api/sessions/<something>, and the two
    # routes that do take {name} there are a GET and a DELETE.
    r.add_post("/api/sessions/kill", h_sessions_kill_all)
    r.add_post("/api/sessions/respawn", h_sessions_respawn_all)
    r.add_get("/api/sessions/{name}", h_session_get)
    r.add_get("/api/sessions/{name}/meta", h_session_meta)
    r.add_get("/api/sessions/{name}/children", h_session_children)
    r.add_post("/api/sessions/{name}/children", h_session_spawn)
    r.add_delete("/api/sessions/{name}/children/{child}", h_session_child_kill)
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
    # Open, and deliberately the only place a client can learn *both* that the
    # daemon is answering and which daemon it is without holding a credential:
    # a browser whose cookie died in the restart still needs to be able to tell
    # "not back yet" from "back, and I must log in again".
    return web.json_response(
        {"status": "ok", "version": __version__, "boot_id": request.app["boot_id"]}
    )


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
            "boot_id": request.app["boot_id"],
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


def _session_cwd(session) -> str:
    """A session's directory, canonical — or '' when it has none.

    Canonical because that is the form a run is keyed by, and empty stays
    empty: an empty cwd must not fall through to the resolver's default, the
    *daemon's* own directory, which would silently hand this session the run
    of whoever is working there.
    """
    raw = session.sdef.cwd
    return cflow_state.resolve_cwd(raw) if raw else ""


def _scope_sessions(manager: SessionManager, cwd: str, scope: str) -> list:
    """The session this run maps 1:1 to (scope == session name), if alive."""
    for session in manager.list():
        if (
            session.sdef.name == scope
            and not session.exited
            and _session_cwd(session) == cwd
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
        explicit = cflow_state.resolve_cwd(explicit)
        scopes = cflow_state.scopes_in(explicit) or [cflow_state.DEFAULT_SCOPE]
        wanted = request.query.get("scope")
        if wanted and not cflow_state.valid_scope(wanted):
            return json_error(400, f"invalid scope: {wanted!r}")
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


def _cflow_entry(
    manager: SessionManager, cwd: str, scope: str, *, reports: bool = True
) -> dict:
    """One (cwd, scope) slot as the dashboard sees it: live status, the recent
    step reports, and any pending start request.

    ``reports=False`` skips reading the run's journal — a whole file, parsed
    per slot per poll — for the callers that show a track rather than prose.
    """
    entry = {
        "cwd": cwd,
        "scope": scope,
        "sessions": _scope_sessions(manager, cwd, scope),
    }
    try:
        payload = cflow_engine.status(cwd, scope=scope)
    except (CflowError, WorkflowError, StateError, OSError) as exc:
        return {**entry, "status": "error", "error": str(exc)}
    if not reports:
        return {**entry, **payload}
    recent = [
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
    return {**entry, **payload, "reports": recent[-_CFLOW_REPORT_TAIL:]}


def _serialize_workflow(wf) -> dict:
    steps = []
    for s in wf.steps.values():
        entry = {
            "id": s.id,
            "title": s.title,
            "instructions": s.instructions,
            "gate": s.gate,
            "ask": _serialize_ask(s.ask),
            "verify": s.verify.command if s.verify else None,
            "next": s.next,
            "select": None,
        }
        if s.select:
            entry["select"] = {
                "prompt": s.select.prompt,
                "chooser": s.select.chooser,
                "from": _serialize_from(s.select.delegate),
                "otherwise": (
                    s.select.delegate.otherwise if s.select.delegate else None
                ),
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
        # `deprecations` deliberately NOT served here. This feeds the run
        # pages, and advice about how a file is written does not belong in
        # front of somebody watching it execute — it would be on screen for
        # every run of every workflow that still spells a gate the old way.
        # `claunch cflow show` is where its author reads it.
        "steps": steps,
    }


def _serialize_from(delegate) -> list:
    """A delegation's preference list as lines a reader can scan.

    The fallback is served separately (``otherwise``) rather than appended
    here: the list is who gets *asked*, and a reader that draws it as a chain
    of responders must not end up drawing the human as one of them.
    """
    if delegate is None:
        return []
    return [c.describe() for c in delegate.candidates]


def _serialize_ask(ask) -> dict | None:
    if ask is None:
        return None
    return {
        "prompt": ask.prompt,
        "from": _serialize_from(ask.delegate),
        "otherwise": ask.delegate.otherwise,
        "timeout": ask.delegate.timeout,
        "on_decline": ask.on_decline,
    }


async def h_cflow_run_detail(request: web.Request) -> web.Response:
    """Everything the dashboard's run page needs: live status, the full
    workflow graph, the step reports, and the run journal."""
    raw = request.query.get("cwd")
    if not raw:
        return json_error(400, "'cwd' query parameter required")
    cwd = cflow_state.resolve_cwd(raw)
    scope = request.query.get("scope") or cflow_state.DEFAULT_SCOPE
    if not cflow_state.valid_scope(scope):
        return json_error(400, f"invalid scope: {scope!r}")
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
    # Same reason, one level up: the scope becomes the *next* path component,
    # and every action below writes through it (the lock, the request file,
    # the run itself).
    scope = str(body.get("scope") or "") or cflow_state.DEFAULT_SCOPE
    if not cflow_state.valid_scope(scope):
        return None, json_error(400, f"invalid scope: {scope!r}")
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
    """What can be started here, each entry saying which file it would run.

    ``origin``/``shadowed`` travel with the path because the dashboard is
    where somebody picks a workflow by name — the one surface where two
    same-named files in two layers look like one thing.
    """
    flows = []
    for found in cflow_state.resolved_workflows(cwd):
        entry = {
            "name": found.name,
            "path": str(found.path),
            "origin": found.origin,
            "shadowed": [str(p) for p in found.shadows],
        }
        try:
            wf = cflow_model.load(found.path)
            entry["description"] = wf.description
            entry["steps"] = wf.step_count()
        except WorkflowError as exc:
            entry["error"] = str(exc)
        flows.append(entry)
    return flows


async def h_cflow_workflows(request: web.Request) -> web.Response:
    """Workflows startable in a directory (project + global) — feeds the
    dashboard's start picker."""
    # An absent cwd means the daemon's own directory, which is exactly what a
    # session created with no directory runs in — so the create form's
    # "(daemon cwd)" asks about the workflows it would really see.
    raw = request.query.get("cwd")
    cwd = cflow_state.resolve_cwd(raw)
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
    # `?session=` asks "which member am I?" — answered as `you`. Optional, so
    # the dashboard poll (which is nobody's session) is unchanged.
    session = str(request.query.get("session") or "")
    return web.json_response(
        {**mm.mesh_info(mesh, session=session),
         "relay": request.app["relay_state"]()}
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
    ref = body.get("ref")
    result = await _mesh_mgr(request).send(
        request.match_info["mesh"],
        sender,
        to,
        text,
        external=bool(body.get("external")),
        type=str(body.get("type") or "say"),
        reply_to=str(body.get("reply_to") or "") or None,
        sections=sections if isinstance(sections, dict) else None,
        ref=ref if isinstance(ref, dict) else None,
    )
    return web.json_response({**result, "relay": request.app["relay_state"]()})


async def h_mesh_history(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", 50))
    except ValueError:
        return json_error(400, "limit must be an integer")
    # Annotated: each message carries who it resolves to *now*, and which of
    # those have actually had it typed in. The sequence view is drawn from
    # this — an arrow that has left but not landed is a different fact from
    # one that landed, and the log alone cannot tell them apart.
    messages = _mesh_mgr(request).history_annotated(request.match_info["mesh"], limit)
    return web.json_response({"messages": messages})


async def h_mesh_owed(request: web.Request) -> web.Response:
    """Who has been asked something and answered nothing — per message."""
    mm = _mesh_mgr(request)
    return web.json_response(mm.owed_report(mm.get(request.match_info["mesh"])))


async def h_mesh_flows(request: web.Request) -> web.Response:
    """What every member of this mesh is *doing*: its cflow run, and the
    workflow graph that run is walking.

    The roster says who is in the room and who may speak to whom; this says
    where each of them has got to. Kept off ``/api/mesh/{mesh}`` on purpose —
    that payload is polled by a page which does not need the graphs, and a
    workflow snapshot is an order of magnitude bigger than a member row.

    Graphs are deduplicated by ``workflow@cwd``: a team of four running the
    same workflow in the same tree is the ordinary case, and shipping that
    graph four times a poll is waste. The first snapshot found under a key
    wins, so two runs of the same name over an edited YAML would share the
    older picture; the drawing side treats a step id it cannot find as
    off-graph rather than trusting the key blindly.

    Remote members carry no run: their state lives on their own daemon, and
    saying so is more use than an empty track that reads as "not started".

    An *exited* session still carries one. A run outlives the agent driving
    it — the state is on disk, and the session is resumable — so a stopped
    member reports where its run got to, flagged ``stopped``; only a member
    whose session is not even a record any more has nothing to show. Reading
    the first as the second is how a run that has real work in it comes to
    look like one that never started.
    """
    mm = _mesh_mgr(request)
    mesh = mm.get(request.match_info["mesh"])
    manager: SessionManager = request.app["manager"]
    known = {s.sdef.name: s for s in manager.list()}

    flows: dict = {}
    workflows: dict = {}
    for handle in sorted(mesh.members):
        member = mesh.members[handle]
        if not mm.is_local_member(mesh, member):
            flows[handle] = {
                "session": member.session,
                "machine": member.machine,
                "remote": True,
            }
            continue
        session = known.get(member.session)
        if session is None:
            flows[handle] = {"session": member.session, "status": "no_session"}
            continue
        cwd = _session_cwd(session)
        if not cwd:
            # A run is keyed by a directory; a session that has none drives no
            # run. Saying so beats attributing it whatever is running in the
            # daemon's own directory, which is where an empty cwd resolves to.
            flows[handle] = {"session": member.session, "status": "no_cwd"}
            continue
        # No reports: the card shows a track, not prose — and this endpoint
        # now reads every member's slot, retired ones included.
        entry = _cflow_entry(manager, cwd, member.session, reports=False)
        flows[handle] = {**entry, "session": member.session}
        if session.exited:
            # Stated, not left to be inferred from an empty `sessions`: it
            # outranks the run's own status on the card, because a recorded
            # position nobody is driving is not progress and cannot be
            # unblocked into any.
            flows[handle]["stopped"] = True
        name = entry.get("workflow")
        if entry.get("status") in (None, "idle", "error") or not name:
            continue
        key = f"{name}@{cwd}"
        flows[handle]["key"] = key
        if key not in workflows:
            try:
                workflows[key] = _serialize_workflow(
                    cflow_state.load_snapshot(cwd, member.session)
                )
            except (WorkflowError, StateError, OSError) as exc:
                # A missing or unreadable snapshot costs the track, not the
                # card: status, step and blockage all still read.
                flows[handle]["graph_error"] = str(exc)
                flows[handle].pop("key", None)
    return web.json_response(
        {"mesh": mesh.name, "flows": flows, "workflows": workflows}
    )


async def h_mesh_nudge(request: web.Request) -> web.Response:
    """Ask a member about its unanswered mail now, without waiting for the
    heartbeat. Optional ``{"body": "..."}`` replaces the heartbeat's text."""
    body = await _json_body(request)
    note = body.get("body")
    if note is not None and not isinstance(note, str):
        return json_error(400, "'body' must be a string")
    result = await _mesh_mgr(request).nudge(
        request.match_info["mesh"],
        request.match_info["handle"],
        str(note or ""),
    )
    return web.json_response(result)


async def h_mesh_owed_dismiss(request: web.Request) -> web.Response:
    """Write off a member's unanswered mail: one message with ``{id}`` on the
    path, the lot without it."""
    mid = request.match_info.get("id")
    result = _mesh_mgr(request).dismiss_owed(
        request.match_info["mesh"],
        request.match_info["handle"],
        [mid] if mid else None,
    )
    return web.json_response(result)


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
        str(body.get("parent") or ""),
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
        lineage=(
            body.get("lineage")
            if isinstance(body.get("lineage"), dict) else None
        ),
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
    """Create a session — and, if asked, everything it needs to start work.

    ``mesh``/``handle``/``connect``, ``workflow``/``context`` and ``task`` are
    optional and composed here, the same way and in the same order the spawn
    endpoint has always composed them for an agent's children (see
    :mod:`claude_launcher.daemon.onboard`). They are checked *before* anything
    is built, so a mistyped mesh is a 400 with no session left behind, and
    *arranged* before it too, so the opening message can be handed to the
    harness on its command line instead of typed into it.

    The response keeps the session's own fields at the top level, as it always
    has, and reports each onboarding leg beside them.
    """
    manager: SessionManager = request.app["manager"]
    body = await _json_body(request)
    body.setdefault("restore", manager.restore_default)
    body.setdefault("name", "")
    try:
        sdef = SessionDef.from_dict(body)
    except (KeyError, ValueError, TypeError) as exc:
        return json_error(400, f"bad session definition: {exc}")
    try:
        session = manager.stage(sdef)
    except ManagerError as exc:
        return json_error(409 if "already exists" in str(exc) else 400, str(exc))
    try:
        result = await _onboard_and_launch(request, session, body)
    except onboard.OnboardError as exc:
        return json_error(400, str(exc))
    return web.json_response({**session.info(), **result}, status=201)


async def _onboard_and_launch(
    request: web.Request, session, body: dict, *, parent: Optional[str] = None
) -> dict:
    """Arrange a staged session, then start it — the shared second half of
    create and spawn.

    The order is the point. Everything checkable is checked before the harness
    exists, so a mistyped mesh costs nothing; the join and the run then happen
    while the session is registered but not running, which is what lets their
    composed opening message go in as an argument rather than be typed into a
    terminal that may not be reading yet. Anything arranged is undone if the
    harness then fails to start, because the name goes straight back into
    circulation and a leftover membership would be inherited by whoever takes
    it next.

    A child's mesh is settled first of all, before the request is validated:
    naming no mesh means *the parent's*, and preflight has to see the mesh
    that decision produced — it is the one that has to exist, hold a free
    handle, and end up in the system prompt.
    """
    manager: SessionManager = request.app["manager"]
    name, cwd = session.sdef.name, session.sdef.cwd
    try:
        if parent:
            await onboard.inherit_mesh(body, parent=parent, mesh_mgr=_mesh_mgr(request))
        plan = onboard.preflight(
            body,
            mesh_mgr=_mesh_mgr(request),
            session_name=name,
            cwd=cwd,
            harness=session.sdef.harness,
            parent=parent or "",
        )
    except Exception:
        manager.discard(name)
        raise
    manager.assign_identity(session, plan.identity)

    report, opening = {}, ""
    if plan.wanted:
        report, opening = await onboard.arrange(
            plan, name=name, cwd=cwd, mesh_mgr=_mesh_mgr(request)
        )
    try:
        manager.launch(session, opening=opening)
    except Exception:
        manager.discard(name)
        await onboard.unwind(report, name=name, cwd=cwd, mesh_mgr=_mesh_mgr(request))
        raise
    onboard.open_with(session, opening)
    return report


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
        manager.get(parent)
    except ManagerError as exc:
        return json_error(404, str(exc))
    try:
        session = manager.stage_child(parent, body)
    except spawn_mod.SpawnDenied as exc:
        return json_error(403, str(exc))
    except (HarnessError, ValueError, TypeError) as exc:
        return json_error(400, f"bad spawn request: {exc}")
    except ManagerError as exc:
        return json_error(409 if "already exists" in str(exc) else 400, str(exc))
    try:
        result = await _onboard_and_launch(request, session, body, parent=parent)
    except onboard.OnboardError as exc:
        return json_error(400, str(exc))
    except (HarnessError, ValueError, TypeError) as exc:
        return json_error(400, f"bad spawn request: {exc}")
    return web.json_response(
        {"session": session.info(), "parent": parent, **result}, status=201
    )


async def h_sessions_clear(request: web.Request) -> web.Response:
    """Drop the records of all exited sessions (``?logs=1`` also deletes their
    captured output). Running sessions are untouched.

    The daemon keeps exited sessions around indefinitely so they stay
    respawnable, so this is the explicit cleanup — nothing else discards them
    in bulk.

    A session a mesh still names is **kept** and reported rather than dropped
    (see :func:`_mesh_holds`) — skipped, not refused, because this is the bulk
    call and one held record should not stop the other nine. ``kept`` says
    which and why, so the omission is visible instead of looking like the
    clear did not take.

    ``?running=1`` widens it from "the exited ones" to "all of them": every
    running session is shut down first — terminated, waited out, force-killed
    if it will not go — and only then are the records dropped. That wait is
    the reason this is one call and not two. :func:`h_sessions_kill_all`
    returns as soon as the signal is sent, and a session that has been sent a
    signal is not yet ``exited``; a clear issued straight after it would skip
    exactly the sessions it was asked to remove, and look like it had done
    nothing. ``stopped`` names what was shut down on the way through.
    """
    manager: SessionManager = request.app["manager"]
    mesh = request.app["mesh"]
    logs = request.query.get("logs") in ("1", "true")
    stopped: List[str] = []
    if request.query.get("running") in ("1", "true"):
        live = [s for s in manager.list() if not s.exited]
        if live:
            # Concurrently: the grace period is per session, and waiting out
            # ten of them in a row is ten graces long for no reason.
            await asyncio.gather(*(s.shutdown() for s in live))
        stopped = [s.sdef.name for s in live]
    kept = [
        {"name": name, "meshes": held}
        for name, held in (
            (s.sdef.name, mesh.meshes_for_session(s.sdef.name))
            for s in manager.list()
            if s.exited
        )
        if held
    ]
    removed = manager.clear(logs=logs, keep=[k["name"] for k in kept])
    return web.json_response(
        {"removed": removed, "kept": kept, "logs": logs, "stopped": stopped}
    )


async def h_sessions_kill_all(request: web.Request) -> web.Response:
    """Kill every running session at once (``?force=1`` to go straight to
    SIGKILL). Exited ones are left alone.

    Records are untouched, which is the whole difference between this and the
    clear above: a killed session reads ``exited`` and stays respawnable,
    exactly as if each terminal's kill button had been pressed in turn. So
    does its mesh row, so there is nothing here for :func:`_mesh_holds` to
    guard — stopping a member is what a member is for.

    One refusal does not stop the rest. This is the bulk call, and a loop that
    gives up on the third of ten leaves an operator with seven sessions they
    asked to stop and no way to tell which; ``failed`` names them instead.
    """
    manager: SessionManager = request.app["manager"]
    force = request.query.get("force") in ("1", "true")
    killed: List[str] = []
    failed: List[dict] = []
    for session in list(manager.list()):
        if session.exited:
            continue
        name = session.sdef.name
        try:
            manager.kill(name, force=force)
        except Exception as exc:  # one refusal must not strand the other nine
            failed.append({"name": name, "error": str(exc)})
        else:
            killed.append(name)
    return web.json_response({"killed": killed, "failed": failed})


async def h_sessions_respawn_all(request: web.Request) -> web.Response:
    """Relaunch every exited session under its own name and definition.

    :func:`h_session_respawn` applied to the whole rail, which is what a rail
    full of exited sessions usually wants: the claude harness comes back with
    ``--resume`` of the conversation pinned at creation, so a laptop that slept
    through a daemon restart comes back as the work that was there rather than
    as a set of fresh, empty terminals.

    In creation order, so a session is back before the ones it spawned — the
    children's records name it, and respawn reads the record.

    Partial results are reported rather than raised, for the same reason as
    the kill above: a name that will not come back is worth knowing, and it is
    no reason to abandon the ones that would have.
    """
    manager: SessionManager = request.app["manager"]
    respawned: List[str] = []
    failed: List[dict] = []
    for session in list(manager.list()):
        if not session.exited:
            continue
        name = session.sdef.name
        try:
            manager.respawn(name)
        except Exception as exc:
            failed.append({"name": name, "error": str(exc)})
        else:
            respawned.append(name)
    return web.json_response({"respawned": respawned, "failed": failed})


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
    cwd = _session_cwd(session)

    harness = harness_registry.registry().get(info.get("harness") or "")
    # Containment, not equality: a session launched with `--worktree` sits in
    # `<repo>/.claude/worktrees/<name>`, which is the workspace the user
    # vouched for with another branch checked out -- not a directory nobody
    # approved. Matching only the exact path reported those as workspace-less,
    # which is the one thing the registry exists to make impossible.
    workspace = workspaces.owning(cwd) if cwd else None
    within = workspaces.subpath(workspace, cwd) if workspace else ""
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
        # Empty when the session is at the workspace root, which is the usual
        # case; the worktree's own directory name when it is not.
        "workspace_subpath": within,
        "role": role,
        "meshes": request.app["mesh"].meshes_for_session(name),
        "cflow": None,
        "workflows": [],
    }
    if cwd:
        body["cflow"] = _cflow_entry(manager, cwd, name)
        body["workflows"] = _startable_workflows(cwd)
    return web.json_response(body)


def _mesh_holds(request: web.Request, name: str) -> List[dict]:
    """The local mesh rows that still name session ``name``.

    Dropping a record one of these names leaves a member pointing at nothing:
    it reads ``missing`` in the roster, it cannot be respawned because respawn
    reads the record that was just deleted, and — because a member's lineage is
    derived from the live session tree rather than stored — the spawn edge that
    said who it worked for silently disappears from the topology. The mesh has
    no way back from that state on its own; only an operator's ``×`` clears it.

    So the record and the row are kept together, and this is the test that
    keeps them so. It is asked here rather than in :class:`SessionManager`
    because the manager knows nothing of meshes, and locally rather than of the
    authority because it must always be answerable — on a mirror, removing a
    member is a call to another daemon that may be unreachable, and a guard
    that can time out is a guard that gets skipped.
    """
    return request.app["mesh"].meshes_for_session(name)


def _mesh_holds_error(name: str, held: List[dict]) -> str:
    where = ", ".join(f"{h['mesh']} (as {h['handle']})" for h in held)
    return (
        f"{name!r} is still a mesh member — {where}. Dropping its record now "
        "would leave that row naming a session nobody can respawn or reach. "
        "Remove it from the mesh first (the roster's ×, or claunch mesh "
        "leave), then clear the record."
    )


async def h_session_delete(request: web.Request) -> web.Response:
    """Kill a running session; drop the record of an exited one (operator).

    The second half is guarded: see :func:`_mesh_holds`. Killing is not — a
    member row is *meant* to outlive the terminal, reading ``exited``.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    force = request.query.get("force") in ("1", "true")
    session = manager.get(name)  # ManagerError -> 400, as it always did
    if session.exited:
        held = _mesh_holds(request, name)
        if held:
            return json_error(409, _mesh_holds_error(name, held))
    session = manager.kill(name, force=force)
    return web.json_response(session.info())


async def h_session_child_kill(request: web.Request) -> web.Response:
    """Retire a session an agent spawned — the counterpart of the POST above.

    Scoped by the route rather than by a flag: ``DELETE /api/sessions/{name}``
    is the operator's, who may end anything, and this one only reaches down
    ``name``'s own subtree. The rule is :meth:`SessionManager.commands`, the
    same one that decides which mesh edges an agent may rewire, so an agent
    ends what it created and nothing else — not a sibling, and not itself,
    which would leave the caller answering from a terminal it just closed.

    On an already-exited child this does **nothing** and says so — the call is
    idempotent. It used to deregister instead, which made the second call the
    destructive one, and a caller reaches for a second call precisely when the
    first *looked* like it had not worked. That is not hypothetical: the first
    real use hit a since-fixed budget bug (the slot did not come back), the
    agent retried the way anyone would, and the retry deleted a record that was
    supposed to stay respawnable. A retry an agent can be induced into must be
    safe, so ending and forgetting are now separate verbs with separate callers
    — forgetting stays the operator's (``clear``), where the mesh guard is.

    What it does not touch either way is the mesh: the member row stays,
    reading ``exited``, because that is what it is, and because a killed child
    is respawnable until somebody clears it.
    """
    manager: SessionManager = request.app["manager"]
    parent = request.match_info["name"]
    child = request.match_info["child"]
    try:
        manager.get(parent)
        target = manager.get(child)
    except ManagerError as exc:
        return json_error(404, str(exc))
    if parent == child:
        return json_error(
            400,
            f"{parent!r} cannot end itself here — this route ends a session "
            "you spawned. An operator can (claunch kill-session).",
        )
    if not manager.commands(parent, child):
        return json_error(
            403,
            f"{parent!r} may not end {child!r}: an agent ends a session it "
            "spawned (or a descendant of one), not a peer. Ask the session "
            "that spawned it, or an operator (claunch kill-session).",
        )
    if target.exited:
        # Already done, so the answer is the same one the first call gave.
        # Reported rather than silent: an agent that asks twice deserves to
        # know the second ask changed nothing, or it will keep asking.
        return web.json_response({**target.info(), "already_exited": True})
    force = request.query.get("force") in ("1", "true")
    session = manager.kill(child, force=force)
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
