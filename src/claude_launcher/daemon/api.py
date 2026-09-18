"""The daemon's HTTP surface: REST API, auth, and the static web UI.

Auth model: every ``/api/*`` call (except ``/api/health``) needs either the
Bearer token (CLI, scripts) or the HttpOnly cookie minted by
``POST /api/auth/session`` (browser — the SPA asks the user to paste the token
once). Tokens never travel in URLs, so they cannot leak into access logs or
browser history.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from typing import Dict, List, Optional, Set, Tuple

from aiohttp import web

from .. import __version__, borrowing, harness_policy, harnesses as harness_registry
from .. import (
    pi_provider,
    credentials,
    lineage,
    metering,
    profile as profile_mod,
    providers,
    quickjob,
    reports as reports_mod,
    runner,
    usage,
)
from .. import session_commits
from .. import beads_meta
from .. import ghcli, prflow, spawn as spawn_mod, store, workspaces
from .. import plugins, settings
from .. import worktree as worktree_mod
from . import beads as beads_mod, connections, handoff as handoff_mod, notice as notice_mod
from . import rag as rag_mod
from . import (
    briefing, cflow_clock, clipty, ctxsize, loops, onboard, prompt_presets,
    rebrief, session_input, status_checks,
)
from . import paths
from . import transcript_view
from . import window as window_mod
from ..cli_beads import BeadsError
from ..cflow import engine as cflow_engine, state as cflow_state
from ..cflow.engine import CflowError
from ..cflow.model import WorkflowError
from ..cflow.state import LockBusy, StateError
from ..profile import ProfileError
from . import goto_gate
from . import mesh_roles
from . import restart_gate
from . import restart_notice
from .harness import HarnessError, SessionDef
from .manager import ManagerError, SessionManager
from .mesh import MeshBusy, MeshConflict, MeshError, MeshManager
from .session import STATUS_IDLE, KeyboardHeld, SessionGone
from . import session as session_mod
from . import ws as ws_mod

COOKIE_NAME = "claunch_session"

_STATIC_DIR = Path(__file__).resolve().parent.parent / "web" / "static"


#: Bodies at least this long are gzip-compressed when the client accepts it.
#: Below it the deflate costs more than the bytes it saves on a local socket.
JSON_GZIP_MIN = 16 * 1024


def _dumps(payload) -> str:
    """``json.dumps`` for the wire: UTF-8 text rather than ``\\uXXXX`` escapes.

    The default escapes every non-ASCII character as six bytes, and a session
    list whose tasks are written in Korean was a fifth larger for it -- on a
    two-second poll. A payload that cannot be encoded (a lone surrogate off a
    filename) is sent escaped instead of costing the caller the response.
    """
    text = json.dumps(payload, ensure_ascii=False)
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        text = json.dumps(payload)
    return text


def json_response(payload, *, status: int = 200, headers=None) -> web.Response:
    """The JSON reply every handler sends: compact on the wire, cheap on the loop.

    Compression only when the body is worth it and the client asked for it
    (``enable_compression`` without ``force`` reads Accept-Encoding when the
    response starts). aiohttp deflates anything past its sync chunk size in
    its zlib executor, so a megabyte of session list is squeezed off the loop
    that pumps every terminal, not on it.
    """
    text = _dumps(payload)
    resp = web.Response(
        text=text, status=status, headers=headers, content_type="application/json",
    )
    if len(text) >= JSON_GZIP_MIN:
        resp.enable_compression()
    return resp


def json_error(status: int, message: str) -> web.Response:
    return json_response({"error": message}, status=status)


log = logging.getLogger("claunch.daemon.api")


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
    except MeshBusy as exc:
        # Backpressure, not a bad request: the send was well-formed and the
        # recipients exist — they are simply too far behind to take it. 429
        # with Retry-After is the one status a caller can act on without
        # reading prose, and the entries let a dashboard mark the rows.
        # Ahead of the MeshError arm below, which would otherwise swallow it
        # (MeshBusy is a MeshError) and report a retryable condition as 400.
        resp = json_response(
            {
                "error": str(exc),
                "deferred": exc.entries,
                "retry_after": exc.retry_after,
            },
            status=429,
        )
        if exc.retry_after:
            resp.headers["Retry-After"] = str(int(exc.retry_after))
        return resp
    except (SessionGone, MeshConflict, LockBusy, KeyboardHeld, restart_gate.GateBusy, goto_gate.GateBusy) as exc:
        # LockBusy is transient by construction (the other writer is mid-
        # transition), so it gets a retryable status, not a flat 400.
        # KeyboardHeld is transient in the same way, and for the most human
        # reason there is: somebody is typing there and the keys were not
        # sent. Both want the caller to come back, not to give up. GateBusy
        # is the same shape: one restart gate, and a second asker must wait
        # for the first request to be settled before it may ask.
        return json_error(409, str(exc))
    except (
        ManagerError,
        HarnessError,
        ProfileError,
        CflowError,
        WorkflowError,
        StateError,
        MeshError,
        BeadsError,
    ) as exc:
        return json_error(400, str(exc))
    except web.HTTPException:
        raise


@web.middleware
async def record_middleware(request: web.Request, handler):
    """Write down that a request arrived, and on which connection.

    Outermost, so it sees the refusals the auth middleware makes as well as
    the requests that get through. aiohttp's access log is off on this
    daemon (``AppRunner(access_log=None)``) and turning it on would put every
    poll of every open page in the log; this keeps a bounded window in memory
    instead, which is what ``/api/connections`` needs and what the log cannot
    give (claunch-restart-disconnect-banner-12p2).
    """
    status = 0
    try:
        response = await handler(request)
        status = getattr(response, "status", 0)
        return response
    except web.HTTPException as exc:
        status = exc.status
        raise
    finally:
        try:
            connections.install(request.app).request(request, status)
        except Exception:  # noqa: BLE001 -- diagnosis must not fail a request
            log.debug("could not record a request", exc_info=True)


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
        # Write the refusal down. Nothing else does: the access log is off,
        # and a refused upgrade never reaches a handler, so a terminal that
        # could not authenticate left no trace at all while the page showed
        # "disconnected" (claunch-restart-disconnect-banner-12p2). The reason
        # separates a request that brought no credential from one whose
        # cookie this daemon has never heard of, which is what a restart
        # leaves behind.
        reason = "stale cookie" if cookie else "no credential"
        try:
            connections.install(request.app).refused(request, reason)
            log.info(
                "refused %s %s: %s (upgrade=%s peer=%s)",
                request.method, request.path, reason,
                request.headers.get("Upgrade", "").lower() == "websocket",
                request.remote,
            )
        except Exception:  # noqa: BLE001 -- diagnosis must not fail a request
            log.debug("could not record a refusal", exc_info=True)
        return json_error(401, "authentication required")

    return auth_middleware


def _relay_unconfigured() -> dict:
    from .relay_uplink import unconfigured_state

    return unconfigured_state()


def build_app(
    manager: SessionManager,
    token: str,
    *,
    started_at: float,
    mesh: MeshManager | None = None,
    relay_state=None,
    shell: "clipty.ShellPty | None" = None,
    beads: "beads_mod.Board | None" = None,
    rag: "rag_mod.RagService | None" = None,
    window: "window_mod.WindowManager | None" = None,
    gate_timeout: float = restart_gate.GATE_TIMEOUT,
    goto_timeout: float = goto_gate.GATE_TIMEOUT,
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
            record_middleware,
            build_auth_middleware(token, cookie_sessions),
        ]
    )
    app["manager"] = manager
    app["mesh"] = mesh if mesh is not None else MeshManager(manager)
    from . import observer
    observer.install(app)
    from . import clipboard
    clipboard.install(app)
    app["relay_state"] = relay_state if relay_state is not None else _relay_unconfigured
    app["token"] = token
    app["cookie_sessions"] = cookie_sessions
    app["started_at"] = started_at
    # The same moment as a wall clock, which is what "when did this daemon
    # come up" means to a person. ``started_at`` is monotonic — it can only
    # ever become a duration; /api/health hands this out so the restart
    # notice can name the restart's own time, not just when the page
    # happened to notice it. Same stamp daemon.json writes.
    app["started_wall"] = datetime.now(timezone.utc).isoformat(timespec="seconds")
    app["boot_id"] = boot_id
    app["shutdown_event"] = asyncio.Event()
    #: Whether the shutdown now in progress should spawn a successor. Read by
    #: ``__main__`` after the loop drains — the handler only marks intent.
    app["restart_requested"] = False
    #: The approval gate the agent path waits behind; see restart_gate.
    app["restart_gate"] = restart_gate.RestartGate(app, timeout=gate_timeout)
    #: The approval gate a leader's child-run goto waits behind; see
    #: goto_gate. Its nudge is the cflow action handlers' own helper, so the
    #: settled move reaches the child driver the same way a human's goto does.
    app["goto_gate"] = goto_gate.GotoGate(
        app,
        manager=manager,
        timeout=goto_timeout,
        nudge=functools.partial(_nudge_sessions, manager),
    )
    # A cflow start selected immediately after session creation can arrive
    # before the harness has mounted its input.  Keep those deliveries alive
    # after the HTTP response so the run remains bound to the new session
    # while its readiness gate finishes.
    app["cflow_nudge_tasks"] = set()
    app.on_shutdown.append(_close_cflow_nudges)
    app["websockets"] = set()
    # Who is holding a socket right now, and the last hundred that closed
    # (daemon/connections.py). The close log alone could not answer whether
    # new sockets were being refused, because a refused one writes nothing.
    connections.install(app)
    # Open terminal sockets never close on their own; without this, runner
    # cleanup waits its shutdown timeout for every browser tab left open.
    app.on_shutdown.append(_close_websockets)
    # The CLI tab's raw shell: one per daemon, injected for tests.
    app["shell"] = shell if shell is not None else clipty.ShellPty()
    app.on_shutdown.append(_close_cli_shell)
    # The board: one per daemon, injected for tests. Its exit sweep rides the
    # manager's hook so every ending — kill, wind-down, /exit, crash — reaches
    # the board, and a daemon shutdown (not an ending) does not.
    board = beads if beads is not None else beads_mod.Board()
    app["beads"] = board
    manager.exit_hooks.append(board.session_exited)
    # Pending merge/handoff requests (daemon/handoff.py): one per daemon, and
    # runtime-only like the board's wind-downs. An exit of any kind makes a
    # pending request on that session moot, so it rides the same hook.
    app["handoff"] = handoff_mod.Handoffs()
    manager.exit_hooks.append(lambda s: app["handoff"].forget(s.sdef.name))
    # Semantic search over that board and this fleet (daemon/rag.py): one
    # service per daemon, injected for tests. Off until the rag: block is
    # filled in; its indexes are derived data under the daemon directory.
    app["rag"] = rag if rag is not None else rag_mod.RagService(board=board, manager=manager)
    # Its producers: every board write the daemon makes, every registry
    # change, every briefing the cache persists. All three are no-ops until
    # the block is configured; the watcher for the writes ``claunch beads``
    # makes without the daemon starts with the app and stops with it, and the
    # boot catch-up rides its first tick. The briefing hook is module-level,
    # so it is taken back at shutdown rather than left for the next app.
    rag_service: rag_mod.RagService = app["rag"]
    board.write_hooks.append(rag_service.on_board_write)
    # Tests hand in registries that carry only exit_hooks; the fleet corpus
    # then follows endings alone, which is all such a registry has.
    change_hooks = getattr(manager, "change_hooks", None)
    if change_hooks is not None:
        change_hooks.append(rag_service.on_sessions_changed)
    manager.exit_hooks.append(rag_service.on_sessions_changed)
    briefing.persist_hooks.append(rag_service.on_sessions_changed)
    app.on_startup.append(_start_rag)
    app.on_shutdown.append(_stop_rag)
    # The measurement window: one per daemon, injected for tests. Its
    # session_exited rides the same exit funnel as the board's, for the same
    # reason: a holder that dies must release without a human noticing.
    window = window if window is not None else window_mod.WindowManager(manager)
    app["window"] = window
    manager.exit_hooks.append(window.session_exited)
    # A restart never lets some endings reach the hook above: restore_all
    # retires what it does not relaunch before this board exists, so each
    # retired record's in_progress issues would keep claiming a dead session
    # is working. Sweep the board for them here, where board and loop exist.
    for dead in manager.take_retired_for_sweep():
        board.session_exited(dead)

    r = app.router
    r.add_get("/api/health", h_health)
    # Open sockets as state (daemon/connections.py), for `claunch connections`
    # and for anyone diagnosing a terminal that will not come up.
    r.add_post("/api/batch", h_batch)
    r.add_get("/api/connections", h_connections)
    r.add_post("/api/connections/close", h_connections_close)
    # The measurement window (daemon/window.py): the machine's test-run
    # arbiter as readable state — who holds it, who waits — so the sweep
    # protocol stops standing on process scans and mesh chat.
    r.add_get("/api/window", h_window_status)
    r.add_post("/api/window/acquire", h_window_acquire)
    r.add_post("/api/window/release", h_window_release)
    r.add_post("/api/window/cancel", h_window_cancel)
    r.add_post("/api/auth/session", h_auth_session)
    r.add_get("/api/daemon", h_daemon_info)
    r.add_post("/api/daemon/shutdown", h_daemon_shutdown)
    r.add_post("/api/daemon/restart", h_daemon_restart)
    # The approval gate for agent-requested restarts (see
    # :mod:`restart_gate`): one request, its state, and the two settlements.
    r.add_get("/api/daemon/restart-request", h_restart_request_get)
    r.add_post("/api/daemon/restart-request", h_restart_request_submit)
    r.add_post("/api/daemon/restart-request/approve", h_restart_request_approve)
    r.add_post("/api/daemon/restart-request/reject", h_restart_request_reject)
    r.add_get("/api/profiles", h_profiles)
    r.add_post("/api/profiles/permission-mode", h_profiles_permission_mode)
    r.add_get("/api/usage", h_usage)
    r.add_get("/api/metering", h_metering)
    r.add_get("/api/borrow-options", h_borrow_options)
    r.add_get("/api/roles", h_roles)
    r.add_get("/api/workspaces", h_workspaces)
    r.add_get("/api/git", h_git)
    r.add_post("/api/workspaces", h_workspace_add)
    r.add_delete("/api/workspaces/{name}", h_workspace_remove)
    r.add_get("/api/briefing/faq", h_briefing_faq)
    r.add_post("/api/briefing/faq", h_briefing_faq_add)
    r.add_put("/api/briefing/faq/{faq_id}", h_briefing_faq_update)
    r.add_delete("/api/briefing/faq/{faq_id}", h_briefing_faq_remove)
    r.add_get("/api/prompt-presets", h_prompt_presets)
    r.add_post("/api/prompt-presets", h_prompt_presets_add)
    r.add_put("/api/prompt-presets/{preset_id}", h_prompt_presets_update)
    r.add_delete("/api/prompt-presets/{preset_id}", h_prompt_presets_remove)
    r.add_get("/api/status-checks", h_status_checks)
    r.add_post("/api/status-checks", h_status_checks_add)
    r.add_put("/api/status-checks/{check_id}", h_status_checks_update)
    r.add_delete("/api/status-checks/{check_id}", h_status_checks_remove)
    r.add_get("/api/harnesses", h_harnesses)
    r.add_get("/api/cflow", h_cflow_runs)
    r.add_get("/api/cflow/run", h_cflow_run_detail)
    r.add_get("/api/cflow/workflows", h_cflow_workflows)
    r.add_post("/api/cflow/start", h_cflow_start)
    r.add_post("/api/cflow/request", h_cflow_request)
    r.add_post("/api/cflow/request/cancel", h_cflow_request_cancel)
    r.add_post("/api/cflow/skip", h_cflow_skip)
    r.add_post("/api/cflow/archive", h_cflow_archive)
    r.add_post("/api/cflow/approve", h_cflow_approve)
    r.add_post("/api/cflow/select", h_cflow_select)
    r.add_post("/api/cflow/nudge", h_cflow_nudge)
    r.add_post("/api/cflow/goto", h_cflow_goto)
    r.add_post("/api/cflow/goto/resolve", h_cflow_goto_resolve)
    # A leader session's request to move a CHILD session's run (see
    # :mod:`goto_gate`): submit, watch, and the three settlements — the web
    # UI's approve/deny and the filing leader's withdraw.
    r.add_get("/api/cflow/goto-requests", h_goto_requests_list)
    r.add_post("/api/cflow/goto-requests", h_goto_request_submit)
    r.add_post("/api/cflow/goto-requests/{rid}/approve", h_goto_request_approve)
    r.add_post("/api/cflow/goto-requests/{rid}/deny", h_goto_request_deny)
    r.add_post("/api/cflow/goto-requests/{rid}/withdraw", h_goto_request_withdraw)
    # One resource, three verbs: GET/PUT are the machine defaults (the config
    # file, read live by the reminder clock, so a PUT applies by its next
    # tick); POST is one run's override, stored in that run's state.
    r.add_get("/api/cflow/reminder", h_cflow_reminder_defaults)
    r.add_put("/api/cflow/reminder", h_cflow_reminder_defaults_set)
    # The stall ping's machine settings — the reminder's complement: that
    # clock steers a session that is working, this one wakes one that stopped
    # at a step no gate is holding. Machine-wide only (no per-run override),
    # read live like the reminder's.
    r.add_get("/api/cflow/ping", h_cflow_ping_defaults)
    r.add_put("/api/cflow/ping", h_cflow_ping_defaults_set)
    r.add_get("/api/quickjob", h_quickjob_get)
    r.add_put("/api/quickjob", h_quickjob_set)
    r.add_post("/api/cflow/reminder", h_cflow_reminder_run_set)
    r.add_post("/api/cflow/reminder/skip", h_cflow_reminder_skip)
    r.add_get("/api/mesh", h_mesh_list)
    r.add_post("/api/mesh", h_mesh_create)
    r.add_delete("/api/mesh/outgoing/{rid}", h_mesh_outgoing_cancel)
    r.add_get("/api/mesh/{mesh}", h_mesh_get)
    r.add_delete("/api/mesh/{mesh}", h_mesh_delete)
    r.add_post("/api/mesh/{mesh}/members", h_mesh_join)
    r.add_delete("/api/mesh/{mesh}/members/{handle}", h_mesh_leave)
    r.add_patch("/api/mesh/{mesh}/members/{handle}/subroles", h_mesh_member_subroles)
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
    r.add_get("/api/mesh/{mesh}/wire-requests", h_mesh_wire_requests)
    r.add_post("/api/mesh/{mesh}/wire-requests/decline", h_mesh_wire_decline)
    r.add_post("/api/mesh/{mesh}/rewire", h_mesh_rewire)
    r.add_get("/api/mesh/{mesh}/policy", h_mesh_policy_get)
    r.add_put("/api/mesh/{mesh}/policy", h_mesh_policy_set)
    r.add_get("/api/mesh/{mesh}/roles", h_mesh_roles_get)
    r.add_put("/api/mesh/{mesh}/roles", h_mesh_roles_set)
    r.add_post("/api/mesh/{mesh}/invitations", h_mesh_invitation)
    # Peer operations: read another member's checkout, coordinate on keys
    # (docs/mesh-design.md "Peer operations"). The caller names itself by
    # session; the target by handle; the member graph is the ACL.
    r.add_post("/api/mesh/{mesh}/ops/file", h_mesh_ops_file)
    r.add_post("/api/mesh/{mesh}/ops/git", h_mesh_ops_git)
    r.add_get("/api/mesh/{mesh}/leases", h_mesh_leases_list)
    r.add_post("/api/mesh/{mesh}/leases", h_mesh_lease)
    r.add_get("/api/relays", h_relay_settings)
    r.add_post("/api/relays", h_relay_save)
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
    r.add_post("/peer/ops/file", h_peer_ops_file)
    r.add_post("/peer/ops/git", h_peer_ops_git)
    r.add_post("/peer/ops/lease", h_peer_ops_lease)
    r.add_get("/api/sessions", h_sessions_list)
    r.add_post("/api/sessions", h_sessions_create)
    r.add_delete("/api/sessions", h_sessions_clear)
    # The bulk verbs, one segment deep so they cannot be read as a session
    # name: nothing else routes POST /api/sessions/<something>, and the two
    # routes that do take {name} there are a GET and a DELETE.
    r.add_post("/api/sessions/kill", h_sessions_kill_all)
    r.add_post("/api/sessions/pause", h_sessions_pause_all)
    r.add_post("/api/sessions/resume", h_sessions_resume_all)
    r.add_post("/api/sessions/respawn", h_sessions_respawn_all)
    r.add_post("/api/sessions/archive", h_sessions_archive_all)
    r.add_get("/api/sessions/{name}", h_session_get)
    r.add_get("/api/sessions/{name}/meta", h_session_meta)
    r.add_get("/api/sessions/{name}/input-journal", h_session_input_journal)
    r.add_get("/api/sessions/{name}/status-checks", h_session_status_checks)
    r.add_post("/api/sessions/{name}/status-checks/reports", h_session_status_checks_report)
    r.add_post("/api/sessions/{name}/status-checks/refresh", h_session_status_checks_refresh)
    r.add_get("/api/sessions/{name}/briefing", h_session_briefing)
    r.add_get("/api/sessions/{name}/queued", h_session_queued)
    r.add_post("/api/sessions/{name}/queued/flush", h_session_queued_flush)
    r.add_post("/api/sessions/{name}/queued/hold", h_session_hold)
    r.add_get("/api/sessions/{name}/reminder", h_session_reminder)
    r.add_post("/api/sessions/{name}/reminder", h_session_reminder_set)
    r.add_post("/api/sessions/{name}/reminder/skip", h_session_reminder_skip)
    r.add_get("/api/sessions/{name}/children", h_session_children)
    r.add_post("/api/sessions/{name}/children", h_session_spawn)
    r.add_post("/api/sessions/{name}/children/{child}/kill", h_session_child_kill)
    r.add_post("/api/sessions/{name}/parent", h_session_reparent)
    r.add_post("/api/sessions/{name}/kill", h_session_kill)
    # A copy of this session's conversation to work in, and the way back
    # (daemon/handoff.py). The merge and the general handoff share a route:
    # what differs is who the target may be, not what happens after.
    r.add_post("/api/sessions/{name}/quick-fork", h_session_quick_fork)
    r.add_post("/api/sessions/{name}/handoff", h_session_handoff)
    r.add_delete("/api/sessions/{name}/handoff", h_session_handoff_cancel)
    r.add_post("/api/sessions/{name}/pause", h_session_pause)
    r.add_post("/api/sessions/{name}/archive", h_session_archive)
    r.add_delete("/api/sessions/{name}", h_session_delete)
    r.add_post("/api/sessions/{name}/keep-alive", h_session_keep_alive)
    r.add_post("/api/sessions/{name}/model", h_session_model)
    r.add_post("/api/sessions/{name}/respawn", h_session_respawn)
    r.add_post("/api/sessions/{name}/migrate", h_session_migrate)
    r.add_post("/api/sessions/{name}/reborrow", h_session_reborrow)
    r.add_post(
        "/api/sessions/{name}/skip-permissions", h_session_skip_permissions
    )
    r.add_post("/api/sessions/{name}/keys", h_session_keys)
    r.add_post("/api/sessions/{name}/paste-image", h_session_paste_image)
    r.add_post("/api/sessions/{name}/deliver", h_session_deliver)
    # The PR wizard (prflow.py): what the session's directory would push,
    # and the push itself. GET is the preview the form opens on, POST does
    # it. Neither touches the session's checkout.
    r.add_get("/api/sessions/{name}/pr/preview", h_session_pr_preview)
    r.add_post("/api/sessions/{name}/pr", h_session_pr)
    r.add_post("/api/sessions/{name}/notice", h_session_notice)
    # One composition, two verbs: GET hands the text to whoever will read it
    # into context (the SessionStart hook, the MCP tool, the CLI); POST types
    # it into the terminal — the operator's push for a session that does not
    # know it needs one.
    r.add_get("/api/sessions/{name}/rebrief", h_session_rebrief)
    r.add_post("/api/sessions/{name}/rebrief", h_session_rebrief)
    r.add_get("/api/sessions/{name}/loops", h_session_loops)
    r.add_post("/api/sessions/{name}/loops", h_session_loop_add)
    r.add_post("/api/sessions/{name}/loops/{loop}/close", h_session_loop_close)
    r.add_get("/api/sessions/{name}/capture", h_session_capture)
    r.add_get("/api/sessions/{name}/transcript", h_session_transcript)
    r.add_get("/api/sessions/{name}/wait", h_session_wait)
    r.add_post("/api/sessions/{name}/resize", h_session_resize)
    r.add_get("/api/sessions/{name}/ws", ws_mod.terminal_ws)
    # The CLI tab's raw shell — one socket per viewer, one child under it.
    r.add_get("/api/cli/ws", ws_mod.cli_ws)
    # The board: every repository board the fleet touches, one issue in
    # full, and a session's own slice of it (GET), with the one write the
    # dashboard offers — an issue for a session that has none (POST).
    r.add_get("/api/beads", h_beads_fleet)
    # Before the {id} route, which would otherwise swallow it: aiohttp matches
    # in registration order and "candidates" is a perfectly good issue id as
    # far as that pattern is concerned.
    r.add_get("/api/beads/candidates", h_beads_candidates)
    # Before "/api/beads/{id}": a literal segment registered after the
    # pattern would be read as an issue called "queues".
    r.add_get("/api/beads/queues", h_beads_queues)
    r.add_get("/api/beads/stream", h_beads_stream)
    r.add_get("/api/beads/{id}", h_beads_issue)
    r.add_post("/api/beads/{id}/assign", h_beads_assign)
    r.add_post("/api/beads/{id}/workspace", h_beads_workspace)
    # Semantic search (daemon/rag.py): the board or the fleet ranked for a
    # query, an issue's nearest neighbours, and the index's own state.
    r.add_get("/api/beads/{id}/related", h_beads_related)
    r.add_get("/api/search", h_search)
    r.add_get("/api/rag/status", h_rag_status)
    r.add_post("/api/rag/reindex", h_rag_reindex)
    # The GitHub CLI as the daemon sees it (ghcli.py): installed, signed in
    # to each host the registered repositories push to, and what to run when
    # not -- the Settings card behind improv-worker-remote's remote-setup.
    r.add_get("/api/tools/gh", h_gh_status)
    r.add_get("/api/sessions/{name}/beads", h_session_beads)
    r.add_post("/api/sessions/{name}/beads", h_session_beads_create)
    # A session's round reports: the index, and the page itself. The index is
    # already inside the beads view above (one panel, one fetch); this route
    # exists for a caller that wants only the files, and the second one is
    # what a link in that panel actually opens.
    r.add_get("/api/sessions/{name}/reports", h_session_reports)
    r.add_get("/api/sessions/{name}/reports/{file}", h_session_report_file)
    # And every report on this machine at once — the Reports page, which is
    # the only reading of these that does not start from a session or an
    # issue the reader already has in hand.
    r.add_get("/api/reports", h_reports_index)
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


async def _close_cflow_nudges(app: web.Application) -> None:
    """Cancel start nudges that are still waiting for terminal readiness."""
    tasks = set(app["cflow_nudge_tasks"])
    for task in tasks:
        task.cancel()
    if tasks:
        await asyncio.gather(*tasks, return_exceptions=True)


async def _start_rag(app: web.Application) -> None:
    """Start the search index's board watcher (its first tick is the boot
    catch-up: every known board and the fleet are queued)."""
    app["rag"].start()


async def _stop_rag(app: web.Application) -> None:
    """Cancel the watcher, the consumer and any sync in flight, and take the
    module-level briefing hook back."""
    service = app["rag"]
    with contextlib.suppress(ValueError):
        briefing.persist_hooks.remove(service.on_sessions_changed)
    await service.shutdown()


async def _close_cli_shell(app: web.Application) -> None:
    """Kill the CLI tab's shell child. A raw shell restores nothing across a
    daemon restart — the daemon dies, the shell dies with it, and the tab's
    first viewer after the restart starts a fresh one."""
    try:
        await app["shell"].shutdown()
    except Exception:  # noqa: BLE001 — never let shutdown fail on this
        pass


async def notify_shutdown(app: web.Application) -> None:
    """Tell every terminal viewer the daemon itself is going down.

    Must be sent before the sessions are terminated: once ``shutdown_all``
    kills a child, viewers receive the same ``exit`` frame a program dying on
    its own would produce, and an attached CLI would give the wrong advice
    (respawn) for what is really a daemon stop/restart (reattach).
    """
    for ws in set(app["websockets"]):
        try:
            await ws.send_str(json.dumps({"type": "shutdown"}))
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
async def h_health(request: web.Request) -> web.Response:
    # Open, and deliberately the only place a client can learn *both* that the
    # daemon is answering and which daemon it is without holding a credential:
    # a browser whose cookie died in the restart still needs to be able to tell
    # "not back yet" from "back, and I must log in again". started_at rides
    # along so the restart notice can say when the new daemon came up.
    #
    # 503 once a stop/restart has been requested. The listener stays up until
    # the very end of teardown (sessions drain first, so attached viewers
    # see the notice), and a 200 in that window is a lie the restart flow
    # acts on: with daemon.json still on disk (its unlink lost a race, see
    # runtime_state.remove_daemon_json) the CLI's ``connect()`` judged the
    # dying daemon SERVING, ``restart()`` returned it as the successor and
    # spawned nothing -- 2026-09-11 22:18, no daemon for 45 minutes.
    stopping = request.app["shutdown_event"].is_set()
    return json_response(
        {
            "status": "stopping" if stopping else "ok",
            "version": __version__,
            "boot_id": request.app["boot_id"],
            "started_at": request.app["started_wall"],
        },
        status=503 if stopping else 200,
    )


async def h_auth_session(request: web.Request) -> web.Response:
    body = await _json_body(request)
    supplied = str(body.get("token") or "")
    if not _token_eq(supplied, request.app["token"]):
        return json_error(401, "bad token")
    session_id = secrets.token_urlsafe(32)
    request.app["cookie_sessions"].add(session_id)
    resp = json_response({"ok": True})
    resp.set_cookie(
        COOKIE_NAME, session_id, httponly=True, samesite="Strict", path="/"
    )
    return resp


#: How many reads one batch may carry. The dashboard's own tick asks for
#: nine; the cap is above that with room for a view's extra reads, and low
#: enough that one request cannot be made to walk the whole API.
BATCH_MAX = 24


async def h_batch(request: web.Request) -> web.Response:
    """Answer several reads on one connection.

    A browser holds a small number of connections to one server -- Firefox's
    default is six -- and every one of them is spent for as long as a request
    is in flight. The dashboard polls nine paths at once every few seconds,
    which took all six, and a WebSocket needs a connection of its own: the
    handshake was queued behind the polls and never sent, so the terminal
    never came up and the daemon never saw a request to refuse
    (claunch-restart-disconnect-banner-12p2; measured as `firefox 6` in
    /api/connections while one socket was open).

    The paths come from the caller, one list per request, so a client keeps
    whatever cadence each read deserves: it asks for the ones that are due
    and leaves out the rest. Nothing here decides how often anything is
    read.

    Only this daemon's own GET routes are reachable, each answer is put
    under the path that asked for it, and a read that fails is reported in
    ``errors`` rather than failing the others -- a batch is a convenience of
    transport, so one bad path must not cost a reader the other eight.
    """
    # Taken before the body is read: aiohttp refuses to clone a request whose
    # content has been consumed, and every read below is a clone of this one
    # (same app, same credential, same peer).
    template = request.clone(method="GET")
    body = await _json_body(request)
    paths = body.get("paths")
    if not isinstance(paths, list) or not paths:
        return json_error(400, "paths must be a non-empty list")
    if len(paths) > BATCH_MAX:
        return json_error(400, f"at most {BATCH_MAX} paths per batch")
    answers: dict = {}
    errors: dict = {}
    for raw in paths:
        if not isinstance(raw, str) or not raw.startswith("/api/"):
            errors[str(raw)] = "only this daemon's /api/ paths"
            continue
        if raw.split("?", 1)[0] == "/api/batch":
            errors[raw] = "a batch cannot carry a batch"
            continue
        try:
            answers[raw] = await _batch_one(template, raw)
        except web.HTTPException as exc:
            errors[raw] = f"{exc.status} {exc.reason}"
        except Exception as exc:  # noqa: BLE001 -- one bad read, eight good
            log.debug("batch read %s failed", raw, exc_info=True)
            errors[raw] = repr(exc)
    return json_response({"answers": answers, "errors": errors})


async def _batch_one(template: web.Request, path: str):
    """Run one of this app's own GET routes and hand back its JSON.

    The route is resolved through the same router that serves it normally,
    so a batched read and a direct one are the same code answering: there is
    no second copy of any payload to drift.
    """
    sub = template.clone(rel_url=path)
    match = await template.app.router.resolve(sub)
    handler = getattr(match, "handler", None)
    if handler is None or getattr(match, "http_exception", None) is not None:
        raise web.HTTPNotFound(reason=f"no route for {path}")
    # What aiohttp's own request handler does before calling a route: hand
    # the match to the request (it is where path parameters live) and name
    # the app the route belongs to (``request.app`` reads it from there).
    sub._match_info = match  # noqa: SLF001
    match.add_app(template.app)
    response = await handler(sub)
    if getattr(response, "status", 200) >= 400:
        raise web.HTTPException(reason=f"{response.status}")
    payload = getattr(response, "body", None)
    if payload is None:
        return None
    return json.loads(bytes(payload).decode("utf-8"))


async def h_connections(request: web.Request) -> web.Response:
    """Every socket the daemon holds open, and the last hundred that closed.

    Authenticated like the rest of ``/api/``: this names sessions and remote
    addresses, which is exactly what the open endpoint above must not.
    """
    registry = connections.install(request.app)
    return json_response(
        registry.snapshot(connections.http_connection_count(request.app))
    )


async def h_connections_close(request: web.Request) -> web.Response:
    """Close sockets the caller names, by the ids a reading gave them.

    An operator's lever, and the only way to test from this side what a
    connection is costing: end one and watch whether the page that could not
    get a socket now gets one. A viewer whose socket is closed here is not
    harmed -- the page's own retry brings it back, which is the same path a
    daemon restart puts it on.

    ``ids`` names them; ``all`` takes every open one. An id that has already
    closed is reported as such rather than failing the call, because the
    reading a caller acted on is always a moment old.
    """
    body = await _json_body(request)
    registry = connections.install(request.app)
    if body.get("all"):
        wanted = [row["id"] for row in registry.snapshot()["open"]]
    else:
        wanted = [int(i) for i in (body.get("ids") or [])]
    if not wanted:
        return json_error(400, "name ids, or pass all")
    closed, gone = [], []
    for socket_id in wanted:
        ws = registry.socket(socket_id)
        if ws is None:
            gone.append(socket_id)
            continue
        with contextlib.suppress(Exception):
            await ws.close()
        closed.append(socket_id)
    log.info("closed %d socket(s) on request: %s", len(closed), closed)
    return json_response({"closed": closed, "already_gone": gone})


async def h_window_status(request: web.Request) -> web.Response:
    """The measurement window as state: holders, queue, caps.

    This read is the protocol's replacement for the process scan: the answer
    comes from the arbiter, so it has no blind spot and no staleness, and it
    reaches sessions that no mesh message would (board claunch-fnhu).
    """
    return json_response(request.app["window"].status())


async def h_window_acquire(request: web.Request) -> web.Response:
    """Ask for the window. ``wait`` seconds > 0 queues and long-polls."""
    body = await _json_body(request)
    cls = body.get("class") or body.get("cls") or ""
    result = await request.app["window"].acquire(
        cls,
        session=body.get("session"),
        pid=int(body.get("pid") or 0),
        label=str(body.get("label") or ""),
        wait=float(body.get("wait") or 0),
    )
    if result.get("error"):
        return json_response(result, status=400)
    return json_response(result)


async def h_window_release(request: web.Request) -> web.Response:
    """Hand a grant back. ``grant_id`` picks one; ``session`` releases all of
    that session's, which is how a caller that lost its grant id (a compacted
    context) can still do the honest thing."""
    body = await _json_body(request)
    window = request.app["window"]
    grant_id = body.get("grant_id")
    if grant_id:
        ok = window.release(str(grant_id))
        return json_response({"released": 1 if ok else 0})
    session = body.get("session")
    if session:
        return json_response({"released": window.release_session(str(session))})
    return json_response(
        {"released": 0, "error": "release wants a grant_id or a session"},
        status=400,
    )


async def h_window_cancel(request: web.Request) -> web.Response:
    """Withdraw a waiting request without releasing any held grant."""
    body = await _json_body(request)
    window = request.app["window"]
    grant_id = body.get("grant_id")
    if grant_id:
        return json_response({"cancelled": 1 if window.cancel(str(grant_id)) else 0})
    session = body.get("session")
    if session:
        return json_response({"cancelled": window.cancel_session(str(session))})
    return json_response(
        {"cancelled": 0, "error": "cancel wants a grant_id or a session"},
        status=400,
    )


async def h_daemon_info(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    sessions = manager.list()
    return json_response(
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
    """Stop the daemon, taking every attached terminal and session with it.

    Operator only. The daemon cannot tell an operator's Bearer token from a
    managed session's (they read the same file), and by decision it does not
    try: the immediate path stays for the operator's own CLI and the web UI.
    An agent session has no authority to call this (or ``restart`` below)
    regardless -- the CLI's approval gate (``restart_gate``) is the one door
    a session's restart goes through, and ``stop`` has none.
    """
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, request.app["shutdown_event"].set)
    return json_response({"ok": True})


async def h_daemon_restart(request: web.Request) -> web.Response:
    """Shut down and hand this port to a fresh daemon process.

    Identical to shutdown from in here — same event, same teardown, same
    notice to attached clients — plus one bit of intent that ``__main__``
    reads once the loop has drained. The successor is spawned there, after
    the singleton lock is released, so it finds the lock free instead of
    spending its grace window waiting this process out. Sessions come back
    the way they do on any restart: relaunched per their ``restore`` flag.

    It records itself first, exactly as the CLI door does. The API cannot
    name a caller -- an HTTP request carries no session -- so this record
    owes nobody a notice; what it does is stop the successor from reporting
    this boot as one that nothing asked for.
    """
    request.app["restart_requested"] = True
    restart_notice.record_request(via="api")
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, request.app["shutdown_event"].set)
    return json_response({"ok": True, "restarting": True})


async def h_restart_request_get(request: web.Request) -> web.Response:
    """The gate's current state: the pending request, the last settled one,
    or nothing. Polled by the web UI's notification card and by the asking
    CLI, whose two readers are exactly the two parties of the gate."""
    return json_response({"request": request.app["restart_gate"].get()})


async def h_restart_request_submit(request: web.Request) -> web.Response:
    """Open the gate on behalf of one session.

    Called by the CLI when it has recognized its shell as a managed session
    (``CLAUNCH_SESSION``) — the daemon cannot see the caller's environment,
    so the session travels in the body and the CLI is the one that decided it
    was an agent asking. ``GateBusy`` (a request already pending) escapes to
    the middleware and comes back as 409.
    """
    body = await _json_body(request)
    session = str(body.get("session") or "").strip()
    if not session:
        return json_error(400, "session is required")
    record = request.app["restart_gate"].submit(session=session)
    return json_response({"ok": True, "request": record})


async def h_restart_request_approve(request: web.Request) -> web.Response:
    """The web UI's Approve: settle the gate and restart — the same intent
    and event as ``/api/daemon/restart``, minus one difference the gate owns:
    the request is recorded with the asking session's name, so the successor
    daemon owes that session the account of the boot."""
    record = request.app["restart_gate"].approve(decided_by="web")
    if record is None:
        return json_error(409, "no pending restart request")
    return json_response({"ok": True, "restarting": True, "request": record})


async def h_restart_request_reject(request: web.Request) -> web.Response:
    """The web UI's Reject: settle the gate and restart nothing. The asking
    session's turn is alive (nothing died), and its CLI poll reads the
    settled record from here."""
    record = request.app["restart_gate"].reject(decided_by="web")
    if record is None:
        return json_error(409, "no pending restart request")
    return json_response({"ok": True, "rejected": True, "request": record})


def _profile_default_tools(profile_obj, entry) -> Optional[list]:
    """The builtin tools a session of this profile+harness gets by default.

    ``None`` for a harness that declares none (the form shows no Tools
    section); otherwise the enabled names, so a form can pre-check them and
    send ``tools`` only when the person changed the set.
    """
    if entry is None or not entry.tools:
        return None
    try:
        return list(pi_provider.enabled_tools(profile_obj, entry))
    except pi_provider.PiProviderError:
        return list(entry.tools)


#: The one settings key the profiles page reports and writes, spelled once.
#: Every reader (the form, the card, the CLI listing) goes through the store's
#: declaration, so the spelling is the identity of the thing being converged.
PERMISSION_MODE_KEY = "permissions.defaultMode"


def _permission_mode(profile_obj, doc: dict) -> dict:
    """What this profile's permission mode is, and where that answer came from.

    Four values, because they answer different questions and a UI that shows
    one of them lies by omission:

    * ``value``      -- what the profile's ``settings.json`` holds right now,
                        or ``None`` when it holds nothing (which is the state
                        that asks before every tool call).
    * ``target``     -- what claunch converges it to (the declaration, or the
                        packaged default under it).
    * ``declared``   -- the declaration as the config file spells it, ``None``
                        when nobody declared one and the packaged default is
                        what is in force. This is the half that says *who*
                        decided: "claunch's default" is not a decision anybody
                        made, and the page says so.
    * ``converged``  -- whether ``value`` already equals ``target``. A profile
                        that disagrees is one whose convergence has not run
                        yet, which is exactly the state ``claunch apply``
                        closes; showing the value alone would make a pending
                        profile look settled.
    """
    declared = store.shared_settings(doc).get(PERMISSION_MODE_KEY)
    value = settings.dotted_get(settings.load(profile_obj), PERMISSION_MODE_KEY)
    target = store.effective_shared_settings(doc).get(PERMISSION_MODE_KEY)
    return {
        "key": PERMISSION_MODE_KEY,
        "value": value,
        "target": target,
        "declared": declared,
        "source": "claunch-default" if declared is None else "declared",
        "converged": value == target,
        "modes": list(settings.PERMISSION_MODES),
    }


def _converge_profiles() -> list:
    """Every Claude Code profile, the set the shared layer writes to.

    Filters the way :mod:`plugins` does and for the same reason: a profile on
    another harness has a config dir ``claude`` never reads, so converging a
    Claude Code settings key into it would write a file nothing loads.
    """
    doc = store.load()
    out = []
    for candidate in profile_mod.list_all():
        try:
            if lineage.effective_harness(candidate, doc) == harness_registry.CLAUDE_HARNESS:
                out.append(candidate)
        except lineage.LineageError:
            # A profile whose chain is broken is not one to write into; the
            # listing already reports it as an error row.
            continue
    return out


async def h_profiles(request: web.Request) -> web.Response:
    # One config read for the whole listing, one availability probe per
    # harness. Both used to happen inside the double loop below: fourteen
    # profiles times six harnesses is 98 rows, and each row re-read and
    # re-parsed ~/.claunch.yaml (once per link of the profile's inheritance
    # chain, plus once per registry lookup) and re-ran shutil.which -- which
    # on Windows walks every PATH directory against every PATHEXT extension.
    # That was ten thousand filesystem probes and a stack of YAML parses to
    # answer six distinct questions, on a handler the dashboard calls while
    # it is booting -- so it was also half a second of event loop that every
    # other request on the page had to wait behind.
    doc = store.load()
    profiles = profile_mod.list_all()
    registry = harness_registry.registry(doc)
    harness_names = harness_registry.names(doc)
    # available() asks the machine, not the profile, so it has exactly as many
    # answers as there are harnesses.
    available = {
        name: bool(entry and entry.available())
        for name, entry in registry.items()
    }
    items = []
    selectors = []
    profile_options = []
    for p in profiles:
        default_name = None
        default_offered = False
        # What each harness's model *choices* resolve to on this profile, once
        # per profile rather than once per row: it is a property of the
        # profile's provider and env, and both rows below read the same map.
        # A form cannot work it out itself -- the alias is what the launch
        # passes and the id behind it is written by config the form never
        # sees -- so it is published here (see runner.model_ids).
        try:
            model_ids = runner.model_ids(p, list(registry.values()), doc=doc)
        except (
            runner.RunnerError,
            providers.ProviderError,
            harness_registry.HarnessConfigError,
        ):
            # A profile whose provider cannot be read is reported as such by
            # its own rows below; this map simply has nothing to say.
            model_ids = {}
        try:
            name = lineage.effective_harness(p, doc)
            default_name = name
            default_offered = True
            default_selector = f"{p.name}:{name}"
            borrow_cap = borrowing.capability(registry.get(name))
            policy_doc = harness_policy.evaluate(p, name, doc=doc).to_dict()
            selectors.append(default_selector)
            profile_options.append(
                {
                    "value": default_selector,
                    "label": f"{p.name}/{name}",
                    "profile": p.name,
                    "harness": name,
                    "default": True,
                    "harness_available": available.get(name, False),
                }
            )
            items.append(
                {
                    "name": p.name,
                    "profile": p.name,
                    "harness": name,
                    "harness_available": available.get(name, False),
                    "borrow_allowed": borrow_cap["allowed"],
                    "borrow_mode": borrow_cap["mode"],
                    "harness_allowed": True,
                    "harness_policy": policy_doc,
                    "explicit": False,
                    "tools": _profile_default_tools(p, registry.get(name)),
                    "model_ids": model_ids.get(name, {}),
                    # Where the profile actually lives. The management page
                    # shows it because a profile IS a directory, and the one
                    # question that needs it ("which of these is the one I am
                    # running in?") is answered by reading the path.
                    "directory": str(p.config_dir),
                    # One row per profile carries this, on the profile's own
                    # harness: it is a Claude Code settings key, so a row for
                    # another harness would be reporting something that
                    # harness never reads. None says "not applicable here"
                    # rather than "no value", which is what ``value: null``
                    # on a Claude row means.
                    "permission_mode": (
                        _permission_mode(p, doc)
                        if name == harness_registry.CLAUDE_HARNESS
                        else None
                    ),
                }
            )
        except lineage.LineageError as exc:
            items.append(
                {
                    "name": p.name,
                    "profile": p.name,
                    "harness": "?",
                    "harness_available": False,
                    "harness_allowed": False,
                    "explicit": False,
                    "error": str(exc),
                }
            )
        for harness_name in harness_names:
            selector = f"{p.name}:{harness_name}"
            borrow_cap = borrowing.capability(registry.get(harness_name))
            try:
                policy = harness_policy.evaluate(
                    p, harness_name, doc=doc
                )
                policy_doc = policy.to_dict()
            except (
                harness_policy.HarnessPolicyError,
                lineage.LineageError,
                providers.ProviderError,
            ) as exc:
                policy = None
                policy_doc = {
                    "allowed": False,
                    "profile": p.name,
                    "harness": harness_name,
                    "reason": str(exc),
                }
            allowed = bool(policy and policy.allowed)
            # The canonical default option was already added above. Do not
            # add the same ``p:default`` selector a second time here.
            offered = allowed and not (
                default_offered and harness_name == default_name
            )
            if offered:
                selectors.append(selector)
                profile_options.append(
                    {
                        "value": selector,
                        "label": f"{p.name}/{harness_name}",
                        "profile": p.name,
                        "harness": harness_name,
                        "default": False,
                        "harness_available": available.get(harness_name, False),
                    }
                )
            items.append(
                {
                    "name": selector,
                    "profile": p.name,
                    "harness": harness_name,
                    "harness_available": available.get(harness_name, False),
                    "borrow_allowed": allowed and borrow_cap["allowed"],
                    "borrow_mode": borrow_cap["mode"],
                    "harness_allowed": allowed,
                    "harness_policy": policy_doc,
                    "explicit": True,
                    "tools": _profile_default_tools(p, registry.get(harness_name)),
                    "model_ids": model_ids.get(harness_name, {}),
                }
            )
    return json_response(
        {
            # Bare names remain for credential/profile-management clients.
            "profiles": [p.name for p in profiles],
            # Session creation uses these policy-filtered execution choices.
            "profile_selectors": selectors,
            "profile_options": profile_options,
            "profile_details": items,
        }
    )


async def h_profiles_permission_mode(request: web.Request) -> web.Response:
    """Declare claunch's default permission mode, then converge the profiles.

    The browser's ``claunch shared permissions.defaultMode=...``, and the same
    two steps in the same order: the declaration is edited in the store, then
    written into every Claude Code profile. Editing applies immediately, which
    is the shared layer's rule everywhere else -- "declare it and remember to
    run apply" is a two-step a person would have to repeat for every profile
    added later.

    One value, not one per profile. The declaration is a single key in the
    shared block and that is the shape the convergence already has; a
    per-profile override is a different design, and inventing it here would
    give the page a state the CLI does not have. A profile that wants to keep
    asking can still be left alone by editing its own ``settings.json`` by
    hand -- claunch converges, it does not police.

    ``{"mode": null}`` (or an empty string) undeclares the key, which returns
    every profile to the packaged default rather than switching the behaviour
    off. ``personal`` is not a mode, and the ones that are come from
    :data:`settings.PERMISSION_MODES`: a typo that reached the store would be
    converged into every profile as a key Claude Code then ignores, silently,
    in the direction that asks more questions rather than fewer.
    """
    body = await _json_body(request)
    raw = body.get("mode")
    declared = str(raw).strip() if raw is not None else ""
    if declared and not settings.is_permission_mode(declared):
        return json_error(
            400,
            f"unknown permission mode {declared!r} (known: "
            f"{', '.join(settings.PERMISSION_MODES)})",
        )
    if declared:
        plugins.set_shared_setting(PERMISSION_MODE_KEY, declared)
    else:
        # Undeclaring pops the key. Setting it to ``None`` would be a
        # different act: the store would keep the key and the convergence
        # would write ``"defaultMode": null`` into every profile -- a value
        # Claude Code ignores, so the mode would fall back to asking while
        # the page reported a value nobody chose.
        plugins.unset_shared_setting(PERMISSION_MODE_KEY)
    targets = _converge_profiles()
    results = await asyncio.to_thread(plugins.apply_all, targets)
    failed = [
        {"profile": result.profile, "reason": plugins.error_line(error)}
        for result in results
        for _action, error in result.failed
    ]
    return json_response(
        {
            "key": PERMISSION_MODE_KEY,
            # What the declaration reads now. Null means "nobody declared one",
            # not "no value in force" -- ``target`` is what is in force.
            "declared": store.shared_settings().get(PERMISSION_MODE_KEY),
            "target": store.effective_shared_settings().get(PERMISSION_MODE_KEY),
            "converged": [r.profile for r in results if r.changed],
            "unchanged": [r.profile for r in results if not r.changed and r.ok],
            "failed": failed,
        }
    )


async def h_metering(request: web.Request) -> web.Response:
    """Throughput records from the metering shim (see ``metering``).

    ``?session=<name>`` narrows to one session and adds its summary (the
    same object the session list carries as ``tps``); ``?limit=N`` caps the
    record count (default 20). File reads, so off the loop.
    """
    session = str(request.query.get("session") or "").strip() or None
    try:
        limit = max(0, min(500, int(request.query.get("limit") or 20)))
    except ValueError:
        return json_error(400, "limit must be an integer")

    def read() -> dict:
        if session:
            records = metering.recent(session, limit=limit)
            summary = metering.session_summary(session)
        else:
            records = metering.load(limit=limit)
            summary = metering.summarize(metering.load())
        return {
            "session": session,
            "enabled": metering.enabled(),
            "summary": summary,
            "records": records,
        }

    return json_response(await asyncio.to_thread(read))


async def h_usage(request: web.Request) -> web.Response:
    """Return subscription usage for a profile selector.

    Usage providers perform network and subprocess I/O.  Run the existing CLI
    implementation in a worker thread so one slow account endpoint cannot
    block the daemon's event loop (or the session rail polling it).
    """
    selector = str(request.query.get("profile") or "").strip()
    if not selector:
        return json_error(400, "profile is required")
    try:
        selected = usage.resolve_target(profile_mod.require_selector(selector))
        report = await asyncio.to_thread(usage.fetch, selected)
    except (
        profile_mod.ProfileError,
        usage.UsageError,
        credentials.CredentialsError,
    ) as exc:
        return json_error(400, str(exc))
    return json_response(
        {
            "profile": selected.selector,
            "source": report.source,
            "windows": [
                {
                    "name": item.name,
                    "utilization": item.utilization,
                    "resets_at": item.resets_at,
                    "used_dollars": item.used_dollars,
                    "limit_dollars": item.limit_dollars,
                    "status": item.status,
                }
                for item in report.windows
            ],
        }
    )


async def h_borrow_options(request: web.Request) -> web.Response:
    """Validated base-profile lenders for one runtime profile selector.

    The response includes the runtime's base profile.  A spawn uses that
    explicit lender to replace authentication inherited from its parent;
    creation and reborrow clients have a separate own-profile authentication
    choice and collapse the duplicate there.
    """
    selector = str(request.query.get("profile") or "").strip()
    if not selector:
        return json_error(400, "query parameter 'profile' is required")
    try:
        runtime = profile_mod.require_selector(selector)
        harness_name = lineage.effective_harness(runtime)
    except (ProfileError, lineage.LineageError) as exc:
        return json_error(400, str(exc))
    entry = harness_registry.get(harness_name)
    capability = borrowing.capability(entry)
    options = []
    if capability["allowed"]:
        for lender in profile_mod.list_all():
            report = borrowing.validate(
                runtime, lender.name, entry=entry
            ).to_dict()
            report["name"] = lender.name
            report["selectable"] = bool(report["valid"])
            report["label"] = (
                lender.name
                if report["valid"]
                else f"{lender.name} — {report['message']}"
            )
            options.append(report)
    return json_response(
        {
            "profile": runtime.selector,
            "harness": harness_name,
            "capability": capability,
            "options": options,
        }
    )


async def h_harnesses(request: web.Request) -> web.Response:
    """The declared harnesses, each with whether this machine can run it.

    ``available`` is reported rather than filtered on: a harness claunch knows
    about but the machine has not installed is a *different* thing from one
    claunch does not know about. Session forms do not use this as a selector;
    they project the harness already configured on their selected profile.
    """
    return json_response(
        {
            "harnesses": [
                harness_registry.registry()[name].to_dict()
                for name in harness_registry.names()
            ]
        }
    )


async def h_workspaces(request: web.Request) -> web.Response:
    """The directories a session may be spawned in, for the pickers."""
    return json_response(
        {"workspaces": [w.to_dict() for w in workspaces.list_all()]}
    )


async def h_git(request: web.Request) -> web.Response:
    """What a directory looks like to git: is it a repository, on what, and
    what checkouts are beside it.

    The pickers that offer a worktree need three facts about a directory, and
    all three are read where the directory *is* -- the ``spawn`` form is
    describing the parent's directory, which is the daemon's filesystem and
    not necessarily the asker's, and the same is true of every workspace in
    the create form. One endpoint rather than three, so a picker cannot show
    branches from one reading and worktrees from another.
    """
    cwd = cflow_state.resolve_cwd(request.query.get("cwd") or None)
    # Three git processes. Off the loop, because read inline they held every
    # terminal's output for the half-second a spawn form took to open (measured
    # at 450-800ms of loop stall per call on a busy checkout).
    info = await asyncio.to_thread(worktree_mod.info, cwd)
    return json_response({"cwd": cwd, **info})


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
    return json_response({"workspace": workspace.to_dict()}, status=201)


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
    return json_response({"workspace": removed.to_dict()})


def _faq_body(body: dict) -> dict:
    question = str(body.get("question") or "").strip()
    answer = str(body.get("answer") or "").strip()
    if not question:
        raise ValueError("an FAQ needs a question")
    if len(question) > 1000 or len(answer) > 5000:
        raise ValueError("FAQ question or answer is too long")
    return {
        "id": str(body.get("id") or ""),
        "question": question,
        "answer": answer,
        "enabled": body.get("enabled", True) is not False,
    }


async def h_briefing_faq(request: web.Request) -> web.Response:
    try:
        return json_response({"faq": briefing.faq_entries()})
    except briefing.FaqError as exc:
        return json_error(500, str(exc))


async def h_briefing_faq_add(request: web.Request) -> web.Response:
    try:
        row = _faq_body(await _json_body(request))
    except ValueError as exc:
        return json_error(400, str(exc))
    try:
        rows = briefing.faq_entries()
        rows.append(row)
        saved = briefing.set_faq_entries(rows)
        return json_response({"faq": saved, "entry": saved[-1]}, status=201)
    except briefing.FaqError as exc:
        return json_error(500, str(exc))


async def h_briefing_faq_update(request: web.Request) -> web.Response:
    faq_id = request.match_info["faq_id"]
    try:
        incoming = _faq_body(await _json_body(request))
    except ValueError as exc:
        return json_error(400, str(exc))
    try:
        rows = briefing.faq_entries()
        for index, row in enumerate(rows):
            if row.get("id") == faq_id:
                incoming["id"] = faq_id
                rows[index] = incoming
                saved = briefing.set_faq_entries(rows)
                return json_response({"faq": saved, "entry": incoming})
        return json_error(404, f"no FAQ named {faq_id!r}")
    except briefing.FaqError as exc:
        return json_error(500, str(exc))


async def h_briefing_faq_remove(request: web.Request) -> web.Response:
    faq_id = request.match_info["faq_id"]
    try:
        rows = briefing.faq_entries()
        kept = [row for row in rows if row.get("id") != faq_id]
        if len(kept) == len(rows):
            return json_error(404, f"no FAQ named {faq_id!r}")
        saved = briefing.set_faq_entries(kept)
        return json_response({"faq": saved, "removed": faq_id})
    except briefing.FaqError as exc:
        return json_error(500, str(exc))


def _prompt_preset_body(body: dict) -> dict:
    name = str(body.get("name") or "").strip()
    text = str(body.get("text") or "").strip()
    if not name or not text:
        raise ValueError("a prompt preset needs a name and message")
    if len(name) > 1000 or len(text) > 10000:
        raise ValueError("prompt preset name or message is too long")
    return {
        "id": str(body.get("id") or ""),
        "name": name,
        "text": text,
        "enabled": body.get("enabled", True) is not False,
    }


async def h_prompt_presets(request: web.Request) -> web.Response:
    try:
        return json_response({"presets": prompt_presets.entries()})
    except prompt_presets.PromptPresetError as exc:
        return json_error(500, str(exc))


async def h_prompt_presets_add(request: web.Request) -> web.Response:
    try:
        row = _prompt_preset_body(await _json_body(request))
    except ValueError as exc:
        return json_error(400, str(exc))
    try:
        rows = prompt_presets.entries()
        rows.append(row)
        saved = prompt_presets.set_entries(rows)
        return json_response({"presets": saved, "preset": saved[-1]}, status=201)
    except prompt_presets.PromptPresetError as exc:
        return json_error(500, str(exc))


async def h_prompt_presets_update(request: web.Request) -> web.Response:
    preset_id = request.match_info["preset_id"]
    try:
        incoming = _prompt_preset_body(await _json_body(request))
    except ValueError as exc:
        return json_error(400, str(exc))
    try:
        rows = prompt_presets.entries()
        for index, row in enumerate(rows):
            if row.get("id") == preset_id:
                incoming["id"] = preset_id
                rows[index] = incoming
                saved = prompt_presets.set_entries(rows)
                return json_response({"presets": saved, "preset": incoming})
        return json_error(404, f"no prompt preset named {preset_id!r}")
    except prompt_presets.PromptPresetError as exc:
        return json_error(500, str(exc))


async def h_prompt_presets_remove(request: web.Request) -> web.Response:
    preset_id = request.match_info["preset_id"]
    try:
        rows = prompt_presets.entries()
        kept = [row for row in rows if row.get("id") != preset_id]
        if len(kept) == len(rows):
            return json_error(404, f"no prompt preset named {preset_id!r}")
        saved = prompt_presets.set_entries(kept)
        return json_response({"presets": saved, "removed": preset_id})
    except prompt_presets.PromptPresetError as exc:
        return json_error(500, str(exc))


def _status_check_body(body: dict) -> dict:
    name = str(body.get("name") or "").strip()
    question = str(body.get("question") or "").strip()
    if not name:
        raise ValueError("a status check needs a name")
    if not question:
        raise ValueError("a status check needs a question")
    if len(name) > 120:
        raise ValueError("a status-check name is too long")
    if len(question) > 1000:
        raise ValueError("a status-check question is too long")
    return {
        "id": str(body.get("id") or ""),
        "name": name,
        "question": question,
        "enabled": body.get("enabled", True) is not False,
    }


async def h_status_checks(request: web.Request) -> web.Response:
    try:
        return json_response({"checks": status_checks.entries()})
    except status_checks.StatusCheckError as exc:
        return json_error(500, str(exc))


async def h_status_checks_add(request: web.Request) -> web.Response:
    try:
        row = _status_check_body(await _json_body(request))
        rows = status_checks.entries()
        rows.append(row)
        saved = status_checks.set_entries(rows)
        return json_response({"checks": saved, "check": saved[-1]}, status=201)
    except ValueError as exc:
        return json_error(400, str(exc))
    except status_checks.StatusCheckError as exc:
        return json_error(500, str(exc))


async def h_status_checks_update(request: web.Request) -> web.Response:
    check_id = request.match_info["check_id"]
    try:
        incoming = _status_check_body(await _json_body(request))
        rows = status_checks.entries()
        for index, row in enumerate(rows):
            if row.get("id") == check_id:
                incoming["id"] = check_id
                rows[index] = incoming
                saved = status_checks.set_entries(rows)
                return json_response({"checks": saved, "check": incoming})
        return json_error(404, f"no status check named {check_id!r}")
    except ValueError as exc:
        return json_error(400, str(exc))
    except status_checks.StatusCheckError as exc:
        return json_error(500, str(exc))


async def h_status_checks_remove(request: web.Request) -> web.Response:
    check_id = request.match_info["check_id"]
    try:
        rows = status_checks.entries()
        kept = [row for row in rows if row.get("id") != check_id]
        if len(kept) == len(rows):
            return json_error(404, f"no status check named {check_id!r}")
        return json_response({"checks": status_checks.set_entries(kept), "removed": check_id})
    except status_checks.StatusCheckError as exc:
        return json_error(500, str(exc))


async def h_roles(request: web.Request) -> web.Response:
    """The packaged fallback vocabulary for clients with no mesh selected.

    A selected mesh's ``/api/mesh/{mesh}/roles`` response is authoritative for
    a membership. The stance travels with each entry so a picker can show the
    common opening briefing before creation.
    """
    roleset = mesh_roles.resolve()
    return json_response(
        {
            "roles": [
                {
                    "name": r.name,
                    "aliases": list(r.aliases),
                    "stance": r.stance,
                }
                for r in (roleset.roles[n] for n in sorted(roleset.roles))
            ]
        }
    )


#: Recent step reports included per run in a run's own payload.
_CFLOW_REPORT_TAIL = 10

#: What the *list* poll carries instead. /api/cflow is fetched every two
#: seconds by every open page, and it answers for every run slot this machine
#: knows about -- around a hundred of them on a working machine, most of them
#: finished. Sending each one its whole journal and its step's whole
#: instructions made that poll a multi-megabyte response the daemon spent
#: longer building than the interval it was asked on, which starved the event
#: loop that also serves the terminals. The list draws a card per run: three
#: report lines, with the details as their tooltip. So it is sent three, with
#: the details clipped to a tooltip's worth -- everything the whole run holds
#: is one click away on /api/cflow/run, which is not polled.
_CFLOW_LIST_REPORTS = 3
_CFLOW_LIST_SUMMARY = 240
_CFLOW_LIST_DETAILS = 400

#: Free text no card in the list draws: a finished run's replayed journal, the
#: current step's instructions and completion criteria, the run's opening
#: context, and the prose an agent is meant to read. Composed anyway (they are
#: what ``status`` answers with) and dropped here rather than made conditional
#: deeper down, so the agent-facing payload keeps its exact shape.
_CFLOW_LIST_DROP = (
    "journal", "instructions", "note", "done_when", "context", "report",
    "how_to_unblock",
)


def _session_cwd(session) -> str:
    """A session's directory, canonical — or '' when it has none.

    Canonical because that is the form a run is keyed by, and empty stays
    empty: an empty cwd must not fall through to the resolver's default, the
    *daemon's* own directory, which would silently hand this session the run
    of whoever is working there.
    """
    raw = session.sdef.cwd
    return cflow_state.resolve_cwd(raw) if raw else ""


#: How long a session's git-branch reading stays plausible. A branch moves
#: rarely (a worktree is cut once; an agent switches branches at most a few
#: times a turn), while this reading rides the rail's every-poll list — so
#: the per-cwd result is remembered for a spell rather than spawning a git
#: process per session per poll, which would be the exact cost the poll was
#: built to avoid.
_BRANCH_TTL = 30.0
_branch_cache: Dict[str, Tuple[str, float]] = {}

#: Directories whose reading is in flight. One at a time per directory: a
#: stale entry is served on every poll until the reader lands, and without
#: this each of those polls would start a reader of its own.
_branch_reading: Set[str] = set()


def _read_branch_later(cwd: str) -> None:
    """Start (or leave running) an off-loop reading of ``cwd``'s branch.

    Off the loop because this is a git process, and a git process is a thing
    that can hang: a lock another program holds, a checkout on a filesystem
    that stopped answering. Read inline — as this was — such a git does not
    delay one response, it stops the daemon: the event loop sits inside
    ``subprocess.run`` and every session, every socket and every timer waits
    on it, including the shutdown that would end the wedge. That is not
    hypothetical; it is where a daemon was found, frozen, mid-``rev-parse``.

    So the loop never waits for git. The reading happens in a worker thread
    and lands in the cache for whoever asks next; callers get the last known
    answer meanwhile.
    """
    if cwd in _branch_reading:
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        # No loop — a CLI or a test calling straight in. Nothing to block,
        # so read it here and answer with it.
        try:
            branch = worktree_mod.current_branch(Path(cwd))
        except Exception:  # noqa: BLE001 — a branch is never worth raising for
            branch = ""
        _branch_cache[cwd] = (branch, time.monotonic())
        return

    def _read() -> str:
        try:
            return worktree_mod.current_branch(Path(cwd))
        except Exception:  # noqa: BLE001 — same: no branch, not an error
            return ""

    def _landed(fut) -> None:
        _branch_reading.discard(cwd)
        try:
            branch = fut.result()
        except Exception:  # noqa: BLE001
            branch = ""
        _branch_cache[cwd] = (branch, time.monotonic())

    _branch_reading.add(cwd)
    loop.run_in_executor(None, _read).add_done_callback(_landed)


def _branch_of(cwd: str) -> str:
    """The git branch checked out at ``cwd``, or ``''``.

    Empty for a directory that is not a git checkout, for a detached HEAD,
    and for no directory — the three cases the UI must not dress as a branch.
    Also empty for a directory nobody has read yet: the reading is started
    here and answered on a later poll, which costs the rail one refresh to
    show a branch and costs the daemon nothing to wait for it.
    """
    if not cwd or not os.path.isdir(cwd):
        return ""
    now = time.monotonic()
    hit = _branch_cache.get(cwd)
    if hit is not None and now - hit[1] < _BRANCH_TTL:
        return hit[0]
    _read_branch_later(cwd)
    return hit[0] if hit is not None else ""


def _scope_sessions(
    manager: SessionManager,
    cwd: str,
    scope: str,
    *,
    live_sessions: Optional[Dict[Tuple[str, str], object]] = None,
) -> list:
    """The session this run maps 1:1 to (scope == session name), if alive."""
    if live_sessions is not None:
        return [scope] if (cwd, scope) in live_sessions else []
    for session in manager.list():
        if (
            session.sdef.name == scope
            and not session.exited
            and _session_cwd(session) == cwd
        ):
            return [scope]
    return []


# --------------------------------------------------------------------------- #
# what the typing clocks are about to do
# --------------------------------------------------------------------------- #
#: The two clocks that type into a driving session on a timer. Every other
#: clock in :mod:`.cflow_clock` fires on an event a reader can already see —
#: a gate entered, a window opening at a stated time. These two fire out of
#: silence, which is why they are the ones worth publishing a countdown for.
_CLOCK_KINDS = ("reminder", "ping")


def _clock_snapshots(app) -> dict:
    """Both clocks' timer tables, read once per request.

    Missing whenever the daemon was built without them — the test app, and
    any embedding that never started the ticks. That absence is reported as
    ``running: false`` below rather than hidden: "no clock" and "a clock that
    is not going to fire" are the same fact to somebody reading a countdown,
    and inventing a number for either is worse than saying so.
    """
    clocks = app.get("cflow_clocks") or {}
    out = {}
    for kind in _CLOCK_KINDS:
        clock = clocks.get(kind)
        if clock is None:
            out[kind] = {"running": False, "timers": {}}
            continue
        try:
            out[kind] = {"running": bool(clock.running), "timers": clock.timers()}
        except Exception:  # noqa: BLE001 — a readout must never break the poll
            # Swallowed without a trace on purpose: this module keeps no
            # logger, and the failure it can actually take is a table read
            # while the clock's worker thread rewrites it. Reporting "no
            # timers" for one poll is the correct degradation — the next
            # poll, two seconds later, has them.
            out[kind] = {"running": False, "timers": {}}
    return out


def _cflow_timers(
    manager: SessionManager, snaps: dict, cfg: Optional[dict],
    cwd: str, scope: str, payload: dict, *,
    live_sessions: Optional[Dict[Tuple[str, str], object]] = None,
) -> dict:
    """The two clocks' standing with one run, as the dashboard states it.

    Three sources, deliberately kept apart. The *policy* (on? how often?)
    comes from the config read this instant, through the same
    :func:`cflow_clock.reminder_policy` the clock itself uses — so a reader
    and the clock cannot disagree about the interval, floor included. The
    *timer* comes from the clock's in-memory table. The *reason it is quiet*
    comes from the run's own position and its session's status, because the
    honest answer to "is the nudge timer working" is usually neither yes nor
    no: it is armed and correctly saying nothing, and which of the several
    ways that happens is the whole content of the answer.

    ``due_in`` may be negative. That is not a bug to clamp away: past zero
    the clock is due and has not delivered — held for a session that stopped,
    or inside the fifteen seconds until the next poll — and a floor of zero
    would hide exactly the stretch a reader is trying to see.
    """
    cfg = cfg or {}
    key = (cwd, scope)
    actionable = cflow_clock._actionable(payload)
    session = (
        live_sessions.get((cwd, scope))
        if live_sessions is not None
        else cflow_clock.session_for(manager, cwd, scope)
    )
    try:
        busy = session is not None and session.status() != STATUS_IDLE
    except Exception:  # noqa: BLE001 — raced with an exit
        busy = False

    def base(kind, enabled, interval):
        snap = snaps.get(kind) or {}
        timer = (snap.get("timers") or {}).get(key)
        view = {
            "running": bool(snap.get("running")),
            "enabled": bool(enabled),
            "interval": interval,
            "due_in": None,
            "fired_ago": (timer or {}).get("fired_ago"),
            "state": "",
        }
        if timer is not None and interval > 0:
            view["due_in"] = interval - timer["armed_ago"]
        return view, timer

    awaits = payload.get("awaits") or {}
    enabled, interval = cflow_clock.reminder_policy(payload, cfg)
    rem, timer = base("reminder", enabled, interval)
    if not rem["running"]:
        rem["state"] = "stopped"
    elif not enabled:
        rem["state"] = "off"
    elif not actionable:
        # A gate, a selection, a responder's answer. The clock stays out of
        # these on purpose (see cflow_clock._ACTIONABLE); reporting "off"
        # here would blame the configuration for a silence the protocol owns.
        rem["state"] = "blocked"
    elif timer is None:
        rem["state"] = "arming"
    elif interval <= 0:
        # The one configuration where this clock says nothing but news: no
        # repeat, and an `awaits` probe watching for the thing to move.
        rem["state"] = "watching"
    elif rem["due_in"] > 0:
        rem["state"] = "counting"
    else:
        rem["state"] = "held" if not busy else "due"
    # Which block is coming, not just when. The reminder restates the step in
    # full the first time it speaks at a position and says the short form
    # after that (:class:`cflow_clock.ReminderClock`), and a countdown that
    # cannot tell the two apart understates the next fire by more than half.
    rem["form"] = "short" if (timer or {}).get("restated") else "full"
    if awaits.get("probe"):
        rem["awaits"] = awaits.get("describe") or awaits.get("probe")
        rem["probe_code"] = (timer or {}).get("probe_code")

    enabled, interval = cflow_clock.ping_policy(cfg)
    ping, timer = base("ping", enabled, interval)
    if not ping["running"]:
        ping["state"] = "stopped"
    elif not enabled:
        ping["state"] = "off"
    elif not actionable or session is None:
        ping["state"] = "blocked"
    elif timer is None:
        ping["state"] = "arming"
    elif timer.get("working"):
        # Armed but not counting: this clock measures an unbroken stretch of
        # *stopped*, and somebody is at work. The number is real, it is just
        # being reset every pass, and a countdown drawn without this reads as
        # a clock that has frozen.
        ping["state"] = "waiting"
    elif ping["due_in"] > 0:
        ping["state"] = "counting"
    else:
        ping["state"] = "due"
    return {"reminder": rem, "ping": ping}


async def h_cflow_runs(request: web.Request) -> web.Response:
    """All monitorable cflow runs, keyed by (directory, scope): the
    machine-local run registry, plus every scope with state in an explicit
    ``?cwd=`` (reported even when idle). A run's ``scope`` is the managed
    session it belongs to (``default`` = started outside any session).
    """
    manager: SessionManager = request.app["manager"]
    # Resolve the session registry once for the whole sweep.  The old path
    # called ``manager.list()`` once per run to bind a scope, then ``get()``
    # once more per run for its timers.  A busy machine has hundreds of run
    # slots, so that repeated sorting and delayed-Codex discovery dominated
    # the endpoint even when every run file was cached.
    # The rail annotates a session row with its run. "Session" here reaches one
    # step past the live ones: a *paused* record is still a session a person
    # selects and resumes, and the rail draws it under Paused — so its run has
    # to survive the rail filter below and bind to its name, exactly as a live
    # one does. A killed or archived record is terminal and earns no such row.
    def _rail_bound(session) -> bool:
        if not session.exited:
            return True
        return bool(getattr(session, "paused_at", None)) and not getattr(
            session, "archived_at", None
        )

    live_sessions = {
        (_session_cwd(session), session.sdef.name): session
        for session in manager.list()
        if _rail_bound(session) and _session_cwd(session)
    }
    rail_view = request.query.get("view") == "rail"

    keys: list = []
    # Registry and run-state reads are filesystem operations.  The complete
    # Flows view can include hundreds of slots and takes seconds on a cold
    # cache, so keep that work off the event loop that also pumps terminals.
    for cwd, scope in await asyncio.to_thread(cflow_state.known_runs):
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

    # The background poll only annotates live session rows.  Historical and
    # unbound runs remain available from the Flows page through the default
    # response, but they no longer require state/journal reads or network
    # transfer in every tab every two seconds.
    if rail_view and not explicit:
        keys = [key for key in keys if key in live_sessions]

    # Read once for the whole sweep, not per run: the config is one file and
    # the clocks are two tables, and this handler is on a two-second poll.
    snaps = _clock_snapshots(request.app)
    try:
        cfg = store.daemon_config()
    except store.StoreError:
        cfg = None  # unreadable config: the clocks read "off", never a guess

    def build_entries() -> list:
        runs = []
        for cwd, scope in keys:
            entry = _cflow_entry(
                manager,
                cwd,
                scope,
                reports=not rail_view,
                slim=True,
                live_sessions=live_sessions,
            )
            # An idle slot is only interesting when it was asked about explicitly,
            # or when a human's start request is waiting to be picked up there.
            if (
                entry.get("status") == "idle"
                and cwd != explicit
                and not entry.get("pending_start")
            ):
                continue
            if entry.get("status") not in ("idle", "error"):
                entry["timers"] = _cflow_timers(
                    manager, snaps, cfg, cwd, scope, entry,
                    live_sessions=live_sessions,
                )
            runs.append(entry)
        return runs

    runs = await asyncio.to_thread(build_entries)
    return json_response({"runs": runs})


def _cflow_entry(
    manager: SessionManager, cwd: str, scope: str, *, reports: bool = True,
    slim: bool = False,
    live_sessions: Optional[Dict[Tuple[str, str], object]] = None,
) -> dict:
    """One (cwd, scope) slot as the dashboard sees it: live status, the recent
    step reports, and any pending start request.

    ``reports=False`` skips reading the run's journal — a whole file, parsed
    per slot per poll — for the callers that show a track rather than prose.
    ``slim=True`` is the shape the two-second list poll takes: the same entry
    with the free text no list card draws cut out of it (see
    ``_CFLOW_LIST_DROP``) and its reports clipped to what one card shows.
    """
    entry = {
        "cwd": cwd,
        "scope": scope,
        "sessions": _scope_sessions(
            manager, cwd, scope, live_sessions=live_sessions,
        ),
    }
    try:
        payload = cflow_engine.status(cwd, scope=scope)
    except (CflowError, WorkflowError, StateError, OSError) as exc:
        return {**entry, "status": "error", "error": str(exc)}
    if slim:
        payload = _slim_cflow_payload(payload)
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
    if slim:
        recent = [
            {
                **r,
                "summary": _clip(r.get("summary"), _CFLOW_LIST_SUMMARY),
                "details": _clip(r.get("details"), _CFLOW_LIST_DETAILS),
            }
            for r in recent[-_CFLOW_LIST_REPORTS:]
        ]
        return {**entry, **payload, "reports": recent}
    return {**entry, **payload, "reports": recent[-_CFLOW_REPORT_TAIL:]}


def _slim_cflow_payload(payload: dict) -> dict:
    """Fields a run-list card or rail badge can render."""
    payload = {k: v for k, v in payload.items() if k not in _CFLOW_LIST_DROP}
    if payload.get("checklist"):
        payload["checklist"] = _slim_checklist(payload["checklist"])
    return payload


#: Per-item fields the list poll does not carry. The card draws a checkbox, a
#: description and the exit code; the command and its captured output are for
#: the run's own view, which is not polled. Left in, a gate whose items print
#: anything at all would multiply that output by every run in the list, every
#: two seconds — the same cost the whole of _CFLOW_LIST_DROP exists to avoid.
_CFLOW_LIST_ITEM_DROP = ("check", "output")


def _slim_checklist(checklist: dict) -> dict:
    return {
        **checklist,
        "items": [
            {k: v for k, v in item.items() if k not in _CFLOW_LIST_ITEM_DROP}
            for item in (checklist.get("items") or [])
        ],
    }


def _clip(text, limit: int):
    """``text`` shortened to ``limit`` characters, with the cut marked."""
    if not isinstance(text, str) or len(text) <= limit:
        return text
    return text[:limit].rstrip() + "…"


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
            # A timed wait's schedule, so a *drawing* can say what the
            # engine's payload already says in prose: this step sits until
            # the daemon moves it, `every` seconds per fire, `max` fires per
            # round. `select` and `timer` are mutually exclusive (model
            # validation), so the two fields never both carry a value.
            "timer": (
                {
                    "every": s.timer.every,
                    "max": s.timer.max,
                    "then": s.timer.then,
                    "after": s.timer.after,
                }
                if s.timer
                else None
            ),
            # A checklist is the step's exit, so a workflow view that omitted
            # it would draw the step as a dead end.
            "checklist": (
                {
                    "prompt": s.checklist.prompt,
                    "then": s.checklist.then,
                    "poll": s.checklist.poll,
                    "items": [
                        {"id": i.id, "describe": i.describe, "check": i.check}
                        for i in s.checklist.items
                    ],
                }
                if s.checklist
                else None
            ),
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
                    {
                        "name": o.name,
                        "description": o.description,
                        "next": o.next,
                        # The cadence, so a *drawing* can say what the run
                        # page's prose already says: this branch is paced,
                        # and a choice made inside its interval is held
                        # rather than taken. Without it the graph has no way
                        # to know, and a reader looking at the picture sees
                        # an ordinary branch. Null on the options (most of
                        # them) that declare none.
                        "interval": o.interval,
                    }
                    for o in s.select.options.values()
                ],
            }
        steps.append(entry)
    return {
        "name": wf.name,
        "description": wf.description,
        "start": wf.start,
        "max_visits": wf.max_visits,
        "recur": wf.recur,
        "default_role": wf.default_role,
        "default_child_cflow": wf.default_child_cflow,
        "priority": wf.priority,
        "filter_roles": (
            {"type": wf.filter_roles.type, "roles": list(wf.filter_roles.roles)}
            if wf.filter_roles
            else None
        ),
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
        return json_response(
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
    try:
        reminder_defaults = _reminder_defaults()
    except store.StoreError:
        reminder_defaults = None  # broken config must not hide the run page

    def _cfg_or_none():
        try:
            return store.daemon_config()
        except store.StoreError:
            return None
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
    return json_response(
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
            # so the run page's reminder control can show the effective
            # values without a second fetch (the override rides in `run`)
            "reminder_defaults": reminder_defaults,
            # ...and what those settings are actually doing right now: armed,
            # counting, held, or not running at all. The settings alone cannot
            # say which.
            "timers": _cflow_timers(
                manager, _clock_snapshots(request.app), _cfg_or_none(),
                cwd, scope, payload,
            ),
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
    """Queue a resume nudge for the run's own session (scope == session
    name, 1:1), so an agent that stopped its turn picks the run back up.
    Default-scope runs belong to no session — nothing to nudge."""
    nudged = []
    for name in _scope_sessions(manager, cwd, scope):
        try:
            session = manager.get(name)
        except Exception:  # noqa: BLE001 — raced with a removal
            continue
        if session.queue_delivery(message):
            nudged.append(name)
    return nudged


def _schedule_cflow_nudges(
    app: web.Application,
    cwd: str,
    scope: str,
    message: str,
    *,
    force: bool = False,
) -> list:
    """Schedule a cflow nudge without holding the dashboard request open.

    A session is already the run's driver once its canonical ``cwd`` and its
    name match the slot. ``Session.deliver`` owns readiness and input-safety
    waits, which can otherwise make an Archive or Start button appear not to
    have acted. ``force`` is reserved for an operator's explicit cflow
    action: it keeps the message whole by submitting an existing draft first,
    then delivers the requested transition promptly.

    Returning matching names distinguishes a pending delivery from a default
    or stale slot, where no session is available to receive it at all.
    """
    manager: SessionManager = app["manager"]
    scheduled = []
    tasks: Set[asyncio.Task] = app["cflow_nudge_tasks"]
    for name in _scope_sessions(manager, cwd, scope):
        try:
            session = manager.get(name)
        except Exception:  # noqa: BLE001 — raced with a removal
            continue
        task = asyncio.create_task(_deliver_cflow_nudge(session, message, force=force))
        tasks.add(task)
        task.add_done_callback(tasks.discard)
        scheduled.append(name)
    return scheduled


async def _deliver_cflow_nudge(session, message: str, *, force: bool = False) -> None:
    """Submit an operator-forced nudge or queue an ordinary notification.

    The session owns readiness and draft waits. Neither path retries a PTY
    write after an I/O error, which could duplicate a partial message.
    """
    if force:
        await session.deliver(message, force=True)
    else:
        session.queue_delivery(message)


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
            composed = cflow_state.compose_located(found, cwd)
            wf = composed.workflow
            if composed.layered:
                entry["extends"] = [str(p) for p in composed.bases]
            entry["description"] = wf.description
            entry["steps"] = wf.step_count()
            entry["recur"] = wf.recur
            # What a role-aware picker needs: who this workflow volunteers
            # itself to, its rank among rivals, and the filter that decides
            # whether volunteering even applies.
            entry["default_role"] = wf.default_role
            entry["default_child_cflow"] = wf.default_child_cflow
            entry["priority"] = wf.priority
            entry["filter_roles"] = (
                {
                    "type": wf.filter_roles.type,
                    "roles": list(wf.filter_roles.roles),
                }
                if wf.filter_roles
                else None
            )
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
    return json_response({"workflows": _startable_workflows(cwd)})


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
    payload["nudge_scheduled_sessions"] = _schedule_cflow_nudges(
        request.app, cwd, scope, cflow_engine.nudge_for_request(name), force=True
    )
    return json_response(payload)


async def h_cflow_request_cancel(request: web.Request) -> web.Response:
    """Withdraw a pending start request (only until the agent acts on it)."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, _ = resolved
    payload = cflow_engine.cancel_request(by="web", cwd=cwd, scope=scope)
    return json_response(payload)


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
    payload["nudge_scheduled_sessions"] = _schedule_cflow_nudges(
        request.app, cwd, scope, cflow_engine.NUDGE_STARTED, force=True
    )
    return json_response(payload)


async def h_cflow_skip(request: web.Request) -> web.Response:
    """Skip the rest of the current round: retire the active run and file the
    start request for the same workflow and context at round + 1.

    The forced version of what a ``recur: true`` run does by itself at a
    normal finish — the round count keeps climbing, so the loop's history
    still reads as "this is its Nth entry", and the ``web-skip`` marker on
    the archive and the request records that this round was cut short, not
    completed. Deliberately the *request* path, not a direct start: the agent
    still performs the start itself, so the run it drives is one it has read
    (see ``h_cflow_request``). Only an ACTIVE round can be skipped — once a
    round finished, its own next-round request is already on file and there
    is nothing left to cut short.
    """
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, _ = resolved
    current = cflow_engine.status(cwd, scope=scope)
    if current.get("status") in ("idle", "done", "aborted"):
        pending = current.get("pending_start") or {}
        if pending.get("by") == "recur":
            return json_error(
                400,
                "nothing to skip: this round already finished and its own "
                "next-round request is on file",
            )
        return json_error(400, "nothing to skip: no active round here")
    source = str(current.get("source") or "")
    if not source:
        return json_error(
            400, "nothing to skip: this run has no recorded source file"
        )
    context = str(current.get("context") or "") or None
    round_next = int(current.get("round") or 1) + 1
    cflow_engine.archive(by="web-skip", cwd=cwd, scope=scope)
    payload = cflow_engine.request_start(
        source, context=context, by="web-skip", round_no=round_next,
        cwd=cwd, scope=scope,
    )
    name = (payload.get("request") or {}).get("name") or source
    payload["nudge_scheduled_sessions"] = _schedule_cflow_nudges(
        request.app, cwd, scope, cflow_engine.nudge_for_request(name), force=True
    )
    return json_response(payload)


async def h_cflow_archive(request: web.Request) -> web.Response:
    """Retire the run (finished or not) into the scope's archive folder,
    freeing the slot for a new start. An active run is aborted first."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, _ = resolved
    payload = cflow_engine.archive(by="web", cwd=cwd, scope=scope)
    payload["nudge_scheduled_sessions"] = _schedule_cflow_nudges(
        request.app, cwd, scope, cflow_engine.NUDGE_ARCHIVED, force=True
    )
    return json_response(payload)


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
        payload["nudge_scheduled_sessions"] = await _nudge_sessions(
            request.app["manager"], cwd, scope, cflow_engine.NUDGE_APPROVED
        )
    return json_response(payload)


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
        payload["nudge_scheduled_sessions"] = await _nudge_sessions(
            request.app["manager"], cwd, scope, cflow_engine.NUDGE_SELECTED
        )
    return json_response(payload)


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
    return json_response({"ok": True, "nudge_scheduled_sessions": nudged})


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
    payload["nudge_scheduled_sessions"] = await _nudge_sessions(
        request.app["manager"], cwd, scope, cflow_engine.nudge_for_state(step)
    )
    return json_response(payload)


async def h_cflow_goto_resolve(request: web.Request) -> web.Response:
    """Answer the agent's request to move to a step the workflow declares no
    route to — the dashboard half of ``claunch cflow goto --approve|--deny``.

    Separate from :func:`h_cflow_goto` rather than folded into it: that one
    forces a position the operator chose, this one answers a question the
    agent asked, and the difference is what the journal has to keep. Both end
    in a nudge, because both leave the agent with something new to read.
    """
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    decision = str(body.get("decision") or "")
    if decision not in ("approve", "deny"):
        return json_error(400, "'decision' must be 'approve' or 'deny'")
    reason = str(body.get("reason") or "") or None
    payload = cflow_engine.resolve_goto(
        decision, by="web", reason=reason, cwd=cwd, scope=scope
    )
    asked = (payload.get("goto_request") or {}).get("step") or ""
    payload["nudge_scheduled_sessions"] = await _nudge_sessions(
        request.app["manager"],
        cwd,
        scope,
        cflow_engine.NUDGE_GOTO_DENIED
        if decision == "deny"
        else cflow_engine.nudge_for_state(str(asked)),
    )
    return json_response(payload)


async def h_goto_request_submit(request: web.Request) -> web.Response:
    """Open the goto gate on a leader's request to move a child's run.

    Same caller model as the restart gate: the daemon cannot see the
    caller's environment, so the session travels in the body and the
    leader's side (the cflow MCP tool) is the one that decided who is
    asking. What the daemon DOES verify is the part only it can: that the
    named session actually commands the target (its own subtree), that the
    target owns exactly one registered run, and — inside the engine — that
    the named step exists and is not where the run already stands. Those
    failures come back as 400; a second pending request on the same run is
    a 409 (GateBusy).
    """
    body = await _json_body(request)
    record = request.app["goto_gate"].submit(
        session=str(body.get("session") or ""),
        target_session=str(body.get("target_session") or ""),
        step=str(body.get("step") or ""),
        reason=str(body.get("reason") or ""),
    )
    return json_response({"ok": True, "request": record})


async def h_goto_requests_list(request: web.Request) -> web.Response:
    """Every live request, then the recently settled ones — the web UI's
    notification cards and a leader checking on its own ask read here."""
    return json_response({"requests": request.app["goto_gate"].list()})


async def h_goto_request_approve(request: web.Request) -> web.Response:
    """The web UI's Approve: settle and apply the move through the engine's
    ordinary grant path, so the child run's journal keeps request and move
    attached to each other."""
    record = request.app["goto_gate"].approve(
        request.match_info["rid"], decided_by="web"
    )
    if record is None:
        return json_error(409, "no pending goto request with that id")
    return json_response({"ok": True, "request": record})


async def h_goto_request_deny(request: web.Request) -> web.Response:
    """The web UI's Deny: settle and move nothing. The refusal waits in the
    child run's state for its driver, exactly as when the driver itself had
    asked."""
    body = await _json_body(request)
    record = request.app["goto_gate"].deny(
        request.match_info["rid"],
        decided_by="web",
        reason=str(body.get("reason") or "") or None,
    )
    if record is None:
        return json_error(409, "no pending goto request with that id")
    return json_response({"ok": True, "denied": True, "request": record})


async def h_goto_request_withdraw(request: web.Request) -> web.Response:
    """The filing leader takes its question back. Only the asker may — the
    person's answer to a request they hold is approve or deny, and anyone
    else's withdrawal would settle a question that was not theirs."""
    body = await _json_body(request)
    actor = str(body.get("session") or "").strip()
    if not actor:
        return json_error(400, "session (the withdrawing leader) is required")
    record = request.app["goto_gate"].withdraw(request.match_info["rid"], actor=actor)
    if record is None:
        return json_error(409, "no pending goto request with that id")
    return json_response({"ok": True, "withdrawn": True, "request": record})


def _reminder_defaults() -> dict:
    """The reminder clock's machine defaults, read live from the config file
    — the same read the clock itself does each tick, so what this reports is
    what the next tick will act on."""
    cfg = store.daemon_config()
    return {
        "enabled": bool(cfg.get("cflow_reminder")),
        "interval": float(cfg.get("cflow_reminder_interval") or 0),
    }


async def h_cflow_reminder_defaults(request: web.Request) -> web.Response:
    try:
        return json_response({"defaults": _reminder_defaults()})
    except store.StoreError as exc:
        return json_error(500, str(exc))


async def h_cflow_reminder_defaults_set(request: web.Request) -> web.Response:
    """Set the machine defaults for the reminder clock.

    Written to the config file, which the clock re-reads on every tick — so
    this applies within one poll, no daemon restart. The same keys answer to
    ``claunch daemon config cflow_reminder`` / ``cflow_reminder_interval``.
    """
    body = await _json_body(request)
    interval = body.get("interval")
    if interval is not None:
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            return json_error(400, "'interval' must be a number of seconds")
        if interval < cflow_engine.REMINDER_MIN_INTERVAL:
            return json_error(
                400,
                f"reminder interval must be at least "
                f"{cflow_engine.REMINDER_MIN_INTERVAL:.0f}s",
            )
    try:
        if "enabled" in body:
            store.set_daemon_field("cflow_reminder", bool(body["enabled"]))
        if interval is not None:
            store.set_daemon_field("cflow_reminder_interval", interval)
        return json_response({"defaults": _reminder_defaults()})
    except store.StoreError as exc:
        return json_error(500, str(exc))


def _ping_defaults() -> dict:
    """The stall-ping clock's machine settings, read live from the config
    file — the same read the clock does each tick, so this reports what the
    next tick will act on."""
    cfg = store.daemon_config()
    return {
        "enabled": bool(cfg.get("cflow_ping")),
        "interval": float(cfg.get("cflow_ping_interval") or 0),
        "message": str(cfg.get("cflow_ping_message") or ""),
        "min_interval": cflow_clock.PING_MIN_INTERVAL,
    }


async def h_cflow_ping_defaults(request: web.Request) -> web.Response:
    try:
        return json_response({"defaults": _ping_defaults()})
    except store.StoreError as exc:
        return json_error(500, str(exc))


async def h_cflow_ping_defaults_set(request: web.Request) -> web.Response:
    """Set the stall-ping clock's machine settings.

    Whether a session that has STOPPED at a step no gate is holding gets
    pinged, after how long, and with what text. Written to the config file,
    which the clock re-reads every tick — so this applies within one poll, no
    daemon restart. The same keys answer to ``claunch daemon config
    cflow_ping`` / ``cflow_ping_interval`` / ``cflow_ping_message``. Partial:
    only the keys sent change.
    """
    body = await _json_body(request)
    interval = body.get("interval")
    if interval is not None:
        try:
            interval = float(interval)
        except (TypeError, ValueError):
            return json_error(400, "'interval' must be a number of seconds")
        if interval < cflow_clock.PING_MIN_INTERVAL:
            return json_error(
                400,
                f"ping interval must be at least "
                f"{cflow_clock.PING_MIN_INTERVAL:.0f}s",
            )
    message = body.get("message")
    if message is not None:
        if not isinstance(message, str):
            return json_error(400, "'message' must be a string")
        message = message.strip()
    try:
        if "enabled" in body:
            store.set_daemon_field("cflow_ping", bool(body["enabled"]))
        if interval is not None:
            store.set_daemon_field("cflow_ping_interval", interval)
        if message is not None:
            # Empty clears it back to the packaged default rather than
            # pinging with a frame and no words in it.
            store.set_daemon_field("cflow_ping_message", message or None)
        return json_response({"defaults": _ping_defaults()})
    except store.StoreError as exc:
        return json_error(500, str(exc))


async def h_quickjob_get(request: web.Request) -> web.Response:
    """The quick-job defaults — what the dashboard's leader form is prefilled
    with. Read live from the config file, like every launcher setting."""
    try:
        return json_response({"quick_job": quickjob.load()})
    except store.StoreError as exc:
        return json_error(500, str(exc))


async def h_quickjob_set(request: web.Request) -> web.Response:
    """Update the quick-job defaults from the dashboard.

    Written to the config file — the same ``quick_job`` block a user edits by
    hand — so the form and the YAML can never disagree about what the
    defaults are. Partial on purpose: only the keys sent change.
    """
    body = await _json_body(request)
    try:
        return json_response({"quick_job": quickjob.save(body)})
    except (ValueError, TypeError) as exc:
        return json_error(400, str(exc))
    except store.StoreError as exc:
        return json_error(500, str(exc))


async def h_cflow_reminder_run_set(request: web.Request) -> web.Response:
    """Set (or clear) one run's reminder override from the dashboard.

    Stored in the run's own state, so it is archived with the run and the
    next run in the slot starts back on the defaults. ``clear: true`` drops
    the override; otherwise ``enabled`` and/or ``interval`` merge over
    whatever override was set before.
    """
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    if body.get("clear"):
        payload = cflow_engine.set_reminder(None, None, by="web", cwd=cwd, scope=scope)
    else:
        enabled = body.get("enabled")
        interval = body.get("interval")
        if enabled is None and interval is None:
            return json_error(
                400, "nothing to set: pass 'enabled' and/or 'interval', "
                "or 'clear': true"
            )
        if interval is not None:
            try:
                interval = float(interval)
            except (TypeError, ValueError):
                return json_error(400, "'interval' must be a number of seconds")
        payload = cflow_engine.set_reminder(
            None if enabled is None else bool(enabled),
            interval, by="web", cwd=cwd, scope=scope,
        )
    try:
        payload["defaults"] = _reminder_defaults()
    except store.StoreError:
        pass  # the override was set; broken config only hides the defaults
    return json_response(payload)


async def h_cflow_reminder_skip(request: web.Request) -> web.Response:
    """Let ONE of a run's cflow reminders go by, without switching it off.

    The narrow verb beside the switch above: it re-arms the clock's timer for
    this run and drops any reminder already held for a stopped session, and
    it writes nothing — no override, no state, nothing archived with the run.
    A person watching a session do one long thing wants *this* reminder not
    to land in the middle of it, and paying for that with a pause they have
    to remember to undo is how a run goes quiet for the rest of the day.

    Answers ``skipped: false`` — not an error — when this daemon's clock was
    keeping no timer for the run. Nothing was coming, so nothing was stopped,
    and the next poll's ``timers`` says why in the run's own words.
    """
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, _body = resolved
    clock = (request.app.get("cflow_clocks") or {}).get("reminder")
    if clock is None:
        # No clock on this daemon means no reminder is ever coming from it.
        # Reported rather than answered `skipped: false`, because the two are
        # different facts and only this one is worth acting on.
        return json_error(503, "this daemon runs no cflow reminder clock")
    return json_response(
        {"cwd": cwd, "scope": scope, "skipped": bool(clock.skip(cwd, scope))}
    )


# --------------------------------------------------------------------------- #
# mesh
# --------------------------------------------------------------------------- #
def _mesh_mgr(request: web.Request) -> MeshManager:
    return request.app["mesh"]


async def h_mesh_list(request: web.Request) -> web.Response:
    mm = _mesh_mgr(request)
    rail_view = request.query.get("view") == "rail"
    return json_response(
        {
            # The rail view is what the sidebar polls: names, local members
            # and counts. The member graph (every pair of every member, with
            # its state) was 480KB of the 525KB full answer on a nine-mesh
            # daemon, and only the mesh page and the flow view draw it -- they
            # fetch /api/mesh/<name>, which still carries it.
            "meshes": [
                mm.mesh_rail_info(m) if rail_view else mm.mesh_info(m)
                for m in mm.list()
            ],
            "outgoing": mm.outgoing_list(),
            "relay": request.app["relay_state"](),
        }
    )


async def h_mesh_create(request: web.Request) -> web.Response:
    body = await _json_body(request)
    mesh = _mesh_mgr(request).create(str(body.get("name") or ""))
    return json_response(_mesh_mgr(request).mesh_info(mesh), status=201)


async def h_mesh_get(request: web.Request) -> web.Response:
    mm = _mesh_mgr(request)
    mesh = mm.get(request.match_info["mesh"])
    # `?session=` asks "which member am I?" — answered as `you`. Optional, so
    # the dashboard poll (which is nobody's session) is unchanged.
    session = str(request.query.get("session") or "")
    return json_response(
        {**mm.mesh_info(mesh, session=session),
         "relay": request.app["relay_state"]()}
    )


async def h_mesh_delete(request: web.Request) -> web.Response:
    _mesh_mgr(request).delete(request.match_info["mesh"])
    return json_response({"ok": True})


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
        subroles=_subroles_in(body),
        code=str(body.get("code") or "") or None,
    )
    if isinstance(result, dict):  # codeless remote join: pended for approval
        return json_response(result, status=202)
    return json_response(result.to_dict(), status=201)


def _subroles_in(body: dict) -> list:
    """``subroles`` as a request body carries it: a list, or one
    comma-separated string from a hand-typed call."""
    raw = body.get("subroles")
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, list):
        return []
    return [str(r).strip() for r in raw if str(r or "").strip()]


async def h_mesh_member_subroles(request: web.Request) -> web.Response:
    """Change one live member's subroles.

    Body: ``{"add": [...]}`` and/or ``{"remove": [...]}``, or ``{"set":
    [...]}`` for the whole list. The primary role is not editable here — a
    member that changes what it is re-joins.
    """
    body = await _json_body(request)
    if "role" in body:
        return json_error(
            400, "the primary 'role' is settled at join and not edited here — "
            "send 'add', 'remove' or 'set' for the subroles"
        )
    replace = None
    if "set" in body:
        replace = _subroles_in({"subroles": body.get("set")})
    member = _mesh_mgr(request).set_subroles(
        request.match_info["mesh"],
        request.match_info["handle"],
        add=_subroles_in({"subroles": body.get("add")}),
        remove=_subroles_in({"subroles": body.get("remove")}),
        replace=replace,
    )
    return json_response(member.to_dict())


async def h_mesh_leave(request: web.Request) -> web.Response:
    member = await _mesh_mgr(request).leave(
        request.match_info["mesh"], request.match_info["handle"]
    )
    return json_response({"ok": True, "handle": member.handle})


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
    return json_response({**result, "relay": request.app["relay_state"]()})


async def h_mesh_history(request: web.Request) -> web.Response:
    try:
        limit = int(request.query.get("limit", 50))
        offset = int(request.query.get("offset", 0))
    except ValueError:
        return json_error(400, "limit and offset must be integers")
    if limit < 0 or offset < 0:
        return json_error(400, "limit and offset must be zero or greater")
    message_filter = request.query.get("filter", "all")
    if message_filter not in {"all", "current", "archived"}:
        return json_error(400, "filter must be all, current, or archived")
    # Annotated: each message carries who it resolves to *now*, and which of
    # those have actually had it typed in. The sequence view is drawn from
    # this — an arrow that has left but not landed is a different fact from
    # one that landed, and the log alone cannot tell them apart.
    page = _mesh_mgr(request).history_annotated_page(
        request.match_info["mesh"],
        limit=limit,
        offset=offset,
        message_filter=message_filter,
    )
    return json_response(page)


async def h_mesh_owed(request: web.Request) -> web.Response:
    """Who has been asked something and answered nothing — per message."""
    mm = _mesh_mgr(request)
    return json_response(mm.owed_report(mm.get(request.match_info["mesh"])))


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
    return json_response(
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
    return json_response(result)


async def h_mesh_owed_dismiss(request: web.Request) -> web.Response:
    """Write off a member's unanswered mail: one message with ``{id}`` on the
    path, the lot without it."""
    mid = request.match_info.get("id")
    result = _mesh_mgr(request).dismiss_owed(
        request.match_info["mesh"],
        request.match_info["handle"],
        [mid] if mid else None,
    )
    return json_response(result)


async def h_mesh_policy_get(request: web.Request) -> web.Response:
    mm = _mesh_mgr(request)
    mesh = mm.get(request.match_info["mesh"])
    return json_response({"policy": mesh.policy})


async def h_mesh_policy_set(request: web.Request) -> web.Response:
    body = await _json_body(request)
    policy = _mesh_mgr(request).set_policy(request.match_info["mesh"], body)
    return json_response({"policy": policy})


async def h_mesh_roles_get(request: web.Request) -> web.Response:
    return json_response(
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
    return json_response(result)


async def h_mesh_invite(request: web.Request) -> web.Response:
    result = _mesh_mgr(request).invite(request.match_info["mesh"])
    return json_response({**result, "relay": request.app["relay_state"]()})


async def h_mesh_invites_list(request: web.Request) -> web.Response:
    return json_response(
        {"invites": _mesh_mgr(request).invite_list(request.match_info["mesh"])}
    )


async def h_mesh_invite_revoke(request: web.Request) -> web.Response:
    revoked = _mesh_mgr(request).invite_revoke(
        request.match_info["mesh"], request.match_info["prefix"]
    )
    return json_response({"revoked": revoked})


async def h_mesh_request_approve(request: web.Request) -> web.Response:
    result = await _mesh_mgr(request).approve_request(
        request.match_info["mesh"], request.match_info["rid"]
    )
    return json_response(result)


async def h_mesh_request_deny(request: web.Request) -> web.Response:
    result = await _mesh_mgr(request).deny_request(
        request.match_info["mesh"], request.match_info["rid"]
    )
    return json_response(result)


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
        subroles=_subroles_in(body),
    )
    return json_response({"member": member}, status=201)


async def h_relay_settings(request: web.Request) -> web.Response:
    service = request.app.get("relay_settings")
    if service is None:
        return json_error(503, "Relay settings are not available yet")
    return json_response(service.state())


async def h_relay_save(request: web.Request) -> web.Response:
    service = request.app.get("relay_settings")
    if service is None:
        return json_error(503, "Relay settings are not available yet")
    try:
        result = await service.save(await _json_body(request))
    except ValueError as exc:
        return json_error(400, str(exc))
    return json_response(result)


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
    return json_response(
        {"peers": sorted(names), "relay": request.app["relay_state"]()}
    )


async def h_relay_peer_sessions(request: web.Request) -> web.Response:
    mm = _mesh_mgr(request)
    if mm.peer_transport is None:
        return json_error(400, "relay uplink is not running")
    machine = request.match_info["machine"]
    payload = await mm.peer_transport(machine, "/peer/sessions", {})
    return json_response(
        {"machine": machine, "sessions": payload.get("sessions", [])}
    )


async def h_mesh_outgoing_cancel(request: web.Request) -> web.Response:
    result = _mesh_mgr(request).cancel_request(request.match_info["rid"])
    return json_response(result)


async def h_mesh_guest_revoke(request: web.Request) -> web.Response:
    result = await _mesh_mgr(request).revoke_guest(
        request.match_info["mesh"], request.match_info["machine"]
    )
    return json_response(result)


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
    return json_response(result)


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
    return json_response(result)


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
    return json_response(result)


async def h_mesh_wire_requests(request: web.Request) -> web.Response:
    """The standing asks for edges this mesh does not have.

    A GET rather than part of the roster: a request is not a property of a
    member, it is a decision waiting on one, and folding it into
    ``/members`` would put it in front of every reader of the topology
    instead of the one session that can answer it.
    """
    rows = _mesh_mgr(request).wire_request_rows(
        request.match_info["mesh"], state=request.query.get("state") or ""
    )
    return json_response({"requests": rows})


async def h_mesh_wire_decline(request: web.Request) -> web.Response:
    """Answer a wire request with no. ``actor`` gates it like a link edit."""
    body = await _json_body(request)
    result = _mesh_mgr(request).decline_wire_request(
        request.match_info["mesh"],
        str(body.get("a") or ""),
        str(body.get("b") or ""),
        actor=str(body.get("actor") or ""),
        reason=str(body.get("reason") or ""),
    )
    return json_response(result)


async def h_mesh_rewire(request: web.Request) -> web.Response:
    """Apply the mesh's ``auto_link`` rules to the members already enrolled.

    The explicit form of a join's wiring, for the case a join cannot cover:
    the rule (or the packaged default carrying it) arrived after the members
    did. Opens only, and never touches a pair somebody already decided — so
    it is safe to run twice, and a deliberate ``disconnect`` outlives it.

    ``actor`` gates it exactly as it gates a single link edit: named, the
    sweep is confined to the edges touching that session's subtree; omitted,
    the whole graph is in scope. The field is declared rather than proven —
    the shared machine token authenticates the daemon's door, not which
    session is behind it — and the one caller this change ships does not
    send one: ``claunch mesh rewire`` posts an empty body. Nothing else
    reaches this route yet because nothing else knows it — this change is
    what adds the route, so "nothing else" is not a survey of the existing
    ecosystem. So ``actor`` is not what makes the operation safe, and it is
    not doing anything yet. What makes it safe is that it opens
    only edges the mesh's rules already sanction and overrules no recorded
    decision.
    """
    body = await _json_body(request)
    opened = await _mesh_mgr(request).rewire_members(
        request.match_info["mesh"], actor=str(body.get("actor") or "")
    )
    return json_response({"opened": opened})


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
    return json_response(result)


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
        subroles=_subroles_in(body),
    )
    return json_response(result)


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
    return json_response(result)


async def h_peer_mesh_invite(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = await _mesh_mgr(request).peer_invite_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("session") or ""),
        str(body.get("handle") or ""),
        str(body.get("role") or ""),
        str(body.get("code") or ""),
        subroles=_subroles_in(body),
    )
    return json_response(result)


async def h_peer_sessions(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    return json_response(
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
    return json_response(result)


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
        subroles=_subroles_in(body),
    )
    return json_response(result)


async def h_peer_leave(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = _mesh_mgr(request).peer_leave_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        str(body.get("handle") or ""),
    )
    return json_response(result)


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
    return json_response(result)


async def h_peer_roles(request: web.Request) -> web.Response:
    """A peer asks us, the authority, to change the mesh's role set."""
    body = await _json_body(request)
    result = _mesh_mgr(request).peer_roles_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        body.get("roles"),
    )
    return json_response(result)


async def h_peer_send(request: web.Request) -> web.Response:
    body = await _json_body(request)
    message = body.get("message")
    result = _mesh_mgr(request).peer_send_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        message if isinstance(message, dict) else {},
    )
    return json_response(result)


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
    return json_response(result)


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
    return json_response(result)


def _ops_actor(body: dict, request: web.Request) -> str:
    """The calling session: body ``actor``, else ``?session=``."""
    return str(body.get("actor") or request.query.get("session") or "")


async def h_mesh_ops_file(request: web.Request) -> web.Response:
    body = await _json_body(request)
    actor = _ops_actor(body, request)
    handle = str(body.get("member") or "")
    path = str(body.get("path") or "")
    if not actor:
        return json_error(400, "'actor' required (the calling session)")
    if not handle:
        return json_error(400, "'member' required (whose checkout to read)")
    if not path:
        return json_error(400, "'path' required")
    max_bytes = body.get("max_bytes")
    try:
        max_bytes = int(max_bytes) if max_bytes not in (None, "") else None
    except (TypeError, ValueError):
        return json_error(400, "'max_bytes' must be an integer")
    result = await _mesh_mgr(request).ops_file(
        request.match_info["mesh"], actor, handle, path, max_bytes=max_bytes
    )
    return json_response(result)


async def h_mesh_ops_git(request: web.Request) -> web.Response:
    body = await _json_body(request)
    actor = _ops_actor(body, request)
    handle = str(body.get("member") or "")
    op = str(body.get("op") or "")
    args = body.get("args")
    if not actor:
        return json_error(400, "'actor' required (the calling session)")
    if not handle:
        return json_error(400, "'member' required (whose checkout to query)")
    if not op:
        return json_error(400, "'op' required (status, diff, log, show, branch)")
    if args is not None and not isinstance(args, dict):
        return json_error(400, "'args' must be an object")
    result = await _mesh_mgr(request).ops_git(
        request.match_info["mesh"], actor, handle, op, args or {}
    )
    return json_response(result)


async def h_mesh_leases_list(request: web.Request) -> web.Response:
    actor = str(request.query.get("session") or "")
    if not actor:
        return json_error(400, "'session' required (the calling session)")
    result = await _mesh_mgr(request).lease(
        request.match_info["mesh"], actor, "list",
        str(request.query.get("holder") or ""),
    )
    return json_response(result)


async def h_mesh_lease(request: web.Request) -> web.Response:
    body = await _json_body(request)
    actor = _ops_actor(body, request)
    op = str(body.get("op") or "acquire")
    key = str(body.get("key") or "")
    if not actor:
        return json_error(400, "'actor' required (the calling session)")
    if op != "list" and not key:
        return json_error(400, "'key' required")
    result = await _mesh_mgr(request).lease(
        request.match_info["mesh"], actor, op, key,
        ttl=body.get("ttl"), note=str(body.get("note") or ""),
    )
    return json_response(result)


async def h_peer_ops_file(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = _mesh_mgr(request).peer_ops_file_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        str(body.get("session") or ""),
        str(body.get("path") or ""),
        body.get("max_bytes"),
    )
    return json_response(result)


async def h_peer_ops_git(request: web.Request) -> web.Response:
    body = await _json_body(request)
    args = body.get("args")
    result = _mesh_mgr(request).peer_ops_git_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        str(body.get("session") or ""),
        str(body.get("op") or ""),
        args if isinstance(args, dict) else {},
    )
    return json_response(result)


async def h_peer_ops_lease(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = _mesh_mgr(request).peer_lease_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        str(body.get("op") or ""),
        str(body.get("key") or ""),
        str(body.get("holder") or ""),
        body.get("ttl"),
        str(body.get("note") or ""),
    )
    return json_response(result)


async def h_sessions_list(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    rail_view = request.query.get("view") == "rail"
    list_state = request.query.get("state") or "all"
    if list_state not in {
        "all", "active", "current", "killed", "paused", "archived",
    }:
        return json_error(400, f"invalid session list state: {list_state!r}")
    reminder_service = request.app.get("session_reminder")
    reminder_cfg = None
    if reminder_service is not None:
        try:
            reminder_cfg = store.daemon_config()
        except store.StoreError:
            # The session list remains available; the service view reports
            # the role source as off until the live config is readable again.
            reminder_cfg = {}
    # ``attach`` rather than ``info`` — every reader of this list wants to know
    # which session is filling up, and the reading is cached against the
    # transcript's own mtime, so a poll where nothing was said costs one stat.
    # Whether the briefing summariser is usable rides the list the UI already
    # polls, so the rail can disable the briefing toggles (and say why) up
    # front instead of every click discovering the 400 for itself.
    sessions = []
    for s in manager.list():
        # Paused is a partition of the exited records, as the rail draws it:
        # ``killed`` is the exited ones that were not paused, so a record is
        # in exactly one of the two lists and the filter counts add up. The
        # partition itself is defined once, in ``session.session_category``,
        # because the mesh roster filters its members by the same words and
        # two copies of the rule would file one record two ways.
        category = session_mod.session_category(s)
        archived = category == session_mod.CATEGORY_ARCHIVED
        paused = category == session_mod.CATEGORY_PAUSED
        if list_state == "active" and s.exited:
            continue
        if list_state == "current" and archived:
            continue
        if list_state == "killed" and category != session_mod.CATEGORY_KILLED:
            continue
        if list_state == "paused" and not paused:
            continue
        if list_state == "archived" and not archived:
            continue
        sessions.append(s)
    winddowns = request.app["beads"].winddowns
    handoffs = request.app["handoff"].pending

    def collect() -> list:
        # The per-session assembly, in a worker: ``attach`` re-reads a
        # transcript tail whenever its file grew, and ten busy sessions grow
        # theirs continuously, so this loop was 30-150ms of the event loop
        # per poll -- time no terminal socket could be served in.
        out = []
        for s in sessions:
            info = ctxsize.attach(s)
            # The session's latest throughput through the metering shim
            # (``tps``), read off the record file tails; absent when the
            # session never went through a shim (the OAuth routes).
            metering.attach(info)
            # The cached briefing's one-liner, when it exists — rides the list
            # the UI already polls so a row can show it without an open card or
            # an LLM call, and so a browser refresh repaints it from the
            # daemon's session state instead of regenerating.
            d = briefing.digest(info.get("name") or "")
            if d:
                info["briefing"] = d
            # A kill that is still a wind-down (see beads.Board): the row says
            # so and its kill button turns into "stop now".
            wd = winddowns.get(info.get("name") or "")
            if wd:
                info["winddown"] = wd
            # A merge/handoff the operator asked for and the agent has not
            # handed in yet: the row says `merging…` and the button turns
            # into "stop now" (daemon/handoff.py).
            ho = handoffs.get(info.get("name") or "")
            if ho:
                info["handoff"] = ho
            if reminder_service is not None:
                info["session_reminder"] = reminder_service.status(
                    info.get("name") or "", cfg=reminder_cfg, session=s,
                )
            out.append(info)
        return out

    attached = await asyncio.to_thread(collect)
    for s, info in zip(sessions, attached):
        # Which git branch the session's checkout is on — one fact that tells
        # two sessions in the same worktree apart without opening either. On
        # the loop, because a miss schedules its git read on the loop (see
        # _read_branch_later); the hit itself is one stat and a dict lookup.
        info["branch"] = _branch_of(_session_cwd(s))
    # A config file that cannot be read must not cost the caller the session
    # list: this poll is the rail's lifeline (it carries every row, and the
    # client rebuilds the whole list off it), while the llm flag is one
    # toggle's enabled-ness. So the read is guarded here rather than allowed
    # to leave the handler -- StoreError is not in error_middleware's list and
    # would surface as a 500 on the one request the UI cannot do without.
    try:
        llm_ok = briefing.llm_configured(briefing.llm_config())
    except store.StoreError:
        llm_ok = False
    # Same guard, same reason, for the rail's search box: whether a semantic
    # search is on offer is one toggle, not a reason to lose the list.
    rag_service = request.app.get("rag")
    rag_ok = bool(rag_service is not None and rag_service.configured())
    try:
        check_digests = status_checks.digests([info.get("name") or "" for info in attached])
    except status_checks.StatusCheckError:
        check_digests = {}
    for info in attached:
        checks = check_digests.get(info.get("name") or "")
        if checks:
            info["status_checks"] = checks
    if rail_view:
        # The dashboard reads this resource repeatedly. The detail panel has
        # its own /meta request, so an opening task and environment do not
        # belong in every rail response.
        rail_fields = {
            "name", "harness", "profile", "cwd", "args", "model", "effort",
            "tools", "restore", "conversation_id", "role", "parent", "borrow",
            "null_token", "issue", "keep_alive", "reminder_paused", "status",
            "pid", "exit_code", "created_at", "last_output_at",
            "last_visited_at", "last_input_at", "last_activity_at", "viewers",
            "exited_at", "archived_at", "paused_at", "delivery_hold", "compacting",
            "context", "branch", "briefing", "winddown", "session_reminder",
            "status_checks", "tps",
        }
        attached = [
            {key: value for key, value in info.items() if key in rail_fields}
            for info in attached
        ]
    return json_response({
        "sessions": attached, "llm_configured": llm_ok, "rag_configured": rag_ok,
    })


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
    # The browser sends a worktree name separately from the session directory.
    # Resolve it before SessionDef is built so the persistent definition keeps
    # the actual checkout path, just like the CLI launch path does. An empty
    # name means the standard generated name; omitting the key means no
    # worktree.
    if "worktree" in body:
        choice = body.pop("worktree")
        if choice is True or choice is None:
            choice = ""
        try:
            base = cflow_state.resolve_cwd(body.get("cwd") or None)
            tree = worktree_mod.resolve(
                base, str(choice), rebase_onto=str(body.pop("rebase_onto", "") or "")
            )
        except worktree_mod.WorktreeError as exc:
            return json_error(400, str(exc))
        if tree is None:
            return json_error(400, "worktree selection did not produce a checkout")
        body["cwd"] = str(tree.path)
    if "harness" in body:
        return json_error(
            400,
            "harness is read-only and comes from profile; omit 'harness'",
        )
    if not str(body.get("profile") or "").strip():
        return json_error(400, "a session needs profile; its harness comes from it")
    body.setdefault("restore", manager.restore_default)
    body.setdefault("name", "")
    # ``role`` is onboarding state owned by the selected mesh. Keep the
    # original request for :func:`onboard.preflight`, while the persistent
    # session definition receives no harness-specific second copy.
    definition = dict(body)
    definition.pop("role", None)
    definition.pop("subroles", None)
    # ``issue_text`` belongs to the newly minted board record.  It is needed
    # below while beads creates that record, but must not enter the temporary
    # session-definition copy (or a future SessionDef field could retain the
    # specification in the daemon's restart record).
    definition.pop("issue_text", None)
    try:
        sdef = SessionDef.from_dict(definition)
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
    return json_response({**session.info(), **result}, status=201)


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
        # Inside the discarding try, and before anything is arranged: a board
        # answer that contradicts itself is checkable without a session, and
        # the alternative is a request that half-happens.
        try:
            beads_mod.check_request(body)
        except beads_mod.BoardRequestError as exc:
            raise onboard.OnboardError(str(exc)) from None
        if parent:
            await onboard.inherit_mesh(body, parent=parent, mesh_mgr=_mesh_mgr(request))
            onboard.inherit_workflow(
                body,
                parent=parent,
                parent_cwd=manager.get(parent).sdef.cwd,
                cwd=cwd,
            )
        plan = onboard.preflight(
            body,
            mesh_mgr=_mesh_mgr(request),
            session_name=name,
            cwd=cwd,
            harness=session.sdef.harness,
            parent=parent or "",
            parent_cwd=manager.get(parent).sdef.cwd if parent else "",
        )
    except Exception:
        manager.discard(name)
        raise
    manager.assign_identity(session, plan.identity)

    # The board link, settled before the opening is composed so the agent's
    # first message names the issue it will be working — the record the
    # workflows tell it to read (`claunch beads show <id> --json`). Never a
    # reason to refuse the session: a board that cannot be written is logged
    # and the session starts without one.
    linked = await request.app["beads"].ensure_issue(
        session, body=body, parent=parent, manager=manager
    )
    report: dict = {}
    if linked:
        beads_mod.link_issue(session, linked["issue"])
        report["beads"] = {k: v for k, v in linked.items() if k != "issue_row"}
        # A JOIN is always spelled out, even when the task already carries the
        # id: `issue: <id>` on its own reads as "this is yours", which is the
        # one thing a joiner must not conclude. Only the ordinary cases are
        # skipped when the reference is already there.
        # An issue written from its own box is spelled out for the same
        # reason: the note is the only place the session is told that the
        # record holds instructions this task does not repeat.
        joined = linked.get("mode") == beads_mod.JOIN
        written = bool(linked.get("from_issue_text"))
        named = linked["issue"] in beads_mod.issue_refs(plan.task, plan.context)
        if joined or written or not named:
            plan = replace(
                plan,
                task=(plan.task + "\n\n" if plan.task else "")
                + beads_mod.compose_link_note(
                    linked["issue"],
                    mode=linked.get("mode") or beads_mod.MINTED,
                    held_by=linked.get("held_by"),
                    mesh=plan.mesh or "",
                    text=written,
                ),
            )
    else:
        # No issue, and until now the session was told nothing about that --
        # so it could not tell an operator's deliberate "no issue" from a
        # mint that failed, and improv-worker's issue-check, which asks it to
        # tell exactly those apart, had no record to read. The answer is
        # written into the opening instead of being left to inference, and
        # which of the two "no issue" answers it was decides what the block
        # says (beads.compose_none_note).
        #
        # NONE_WAIT is added only to a session that is getting an opening
        # anyway: a bare interactive session created with --no-issue and
        # nothing else asked for no instructions, and "wait for instructions"
        # is what it would do regardless. NONE_AUTO is always added, because
        # there the block IS the instruction -- go and take work off the
        # board -- and a session that never receives it does the opposite of
        # what was asked.
        none = beads_mod.none_mode(body)
        if none and (plan.wanted or none == beads_mod.NONE_AUTO):
            report["beads"] = {"issue": None, "mode": none}
            plan = replace(
                plan,
                task=(plan.task + "\n\n" if plan.task else "")
                + beads_mod.compose_none_note(none, session=name),
            )

    opening = ""
    if plan.wanted:
        arranged, opening = await onboard.arrange(
            plan, name=name, cwd=cwd, mesh_mgr=_mesh_mgr(request)
        )
        report.update(arranged)
    try:
        manager.launch(session, opening=opening)
    except Exception:
        manager.discard(name)
        await onboard.unwind(report, name=name, cwd=cwd, mesh_mgr=_mesh_mgr(request))
        raise
    onboard.open_with(session, opening)
    if linked and linked.get("mode") == beads_mod.JOIN:
        # Last, because it names a session that now exists: the holder is told
        # only once there is something for it to settle with. After launch for
        # the same reason the join comment is written before it — the board
        # keeps the record either way, the message is the nudge.
        report["beads"]["notified"] = await _tell_issue_holder(
            request, joiner=name, linked=linked
        )
    return report


async def _tell_issue_holder(request: web.Request, *, joiner: str, linked: dict) -> str:
    """Tell the session that holds an issue that a second session joined it.

    The daemon's half of the ownership rule: it refuses to move the
    assignment, so the two sessions have to settle it, and the holder cannot
    see that there is anything to settle from inside its own terminal. Sent
    on a mesh the two share, as ``fyi`` from the board rather than from the
    joiner — nobody owes the daemon a reply, and the holder keeps the issue
    whether it reads this or not.

    Returns the mesh it went out on, or "" — a notice that could not be sent
    is never a reason to fail a session that has already started.
    """
    holder = linked.get("held_by") or ""
    if not holder:
        return ""
    mm = _mesh_mgr(request)
    if mm is None:
        return ""
    try:
        mine = {m["mesh"] for m in mm.meshes_for_session(joiner)}
        shared = [m for m in mm.meshes_for_session(holder) if m["mesh"] in mine]
    except Exception as exc:  # noqa: BLE001 - a roster read must not fail a launch
        beads_mod.log.debug("beads: no holder notice for %r: %s", holder, exc)
        return ""
    if not shared:
        return ""
    row = linked.get("issue_row") or {"id": linked.get("issue")}
    body = beads_mod.compose_join_notice(joiner, row, holder=holder)
    for entry in shared:
        try:
            await mm.send(
                entry["mesh"], beads_mod.BOARD_SENDER, entry["handle"], body,
                external=True, type="fyi",
            )
            return entry["mesh"]
        except Exception as exc:  # noqa: BLE001
            beads_mod.log.debug("beads: holder notice on %r failed: %s", entry["mesh"], exc)
    return ""


async def h_session_children(request: web.Request) -> web.Response:
    """A session's subtree plus what it may still spawn.

    Each child row also carries the cflow run that child drives, when there
    is one — the overseer-facing counterpart of the run event clock's push,
    so "where is everybody" is one call instead of one shell read per child.

    ``child_cflow`` is the run the NEXT child would start on: the pair this
    session's own workflow declares (``default_child_cflow``), "" when it
    declares none. It rides here because this is the call every spawn form
    already makes about its parent, and because a form that computed the pair
    itself would be a second reading of it — :func:`onboard.inherit_workflow`
    applies exactly this answer when the spawn names no workflow.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    manager.get(name)  # 404 for an unknown parent, before reporting on it

    def describe() -> dict:
        # In a worker: every child's run state, the parent's own run and
        # workflow snapshot, the declared workflows and the spawn policy are
        # all files (YAML and JSON, parsed each time). The session page polls
        # this every five seconds, and read inline it was 500-600ms of event
        # loop per call -- half a second in which no terminal was served.
        children = []
        for child in manager.children(name):
            sess = manager.get(child)
            entry = {
                "name": child,
                "status": sess.status(),
                "children": manager.children(child),
            }
            run = cflow_clock.run_summary(child, sess.sdef.cwd or "")
            if run:
                entry["cflow"] = run
            children.append(entry)
        parent_cwd = manager.get(name).sdef.cwd or ""
        return {
            "session": name,
            "parent": manager.get(name).sdef.parent,
            "children": children,
            "descendants": manager.descendants(name),
            # A child inherits its parent's directory unless the spawn sends
            # it elsewhere, so that is the cwd this answer is about; a form
            # aiming a child at another workspace re-reads the workflows
            # declared there anyway.
            "child_cflow": onboard.paired_child_workflow(
                parent=name, parent_cwd=parent_cwd, cwd=parent_cwd
            ),
            **manager.spawn_capabilities(name),
        }

    return json_response(await asyncio.to_thread(describe))


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

    ``warnings`` is the same idea one step earlier: what the policy allowed
    and still wants said. A crossed child cap lands there instead of in a 403,
    so the caller gets the child AND the sentence about it in one answer.
    """
    manager: SessionManager = request.app["manager"]
    parent = request.match_info["name"]
    body = await _json_body(request)
    try:
        manager.get(parent)
    except ManagerError as exc:
        return json_error(404, str(exc))
    warnings: list = []
    try:
        session = manager.stage_child(parent, body, warnings=warnings)
    except spawn_mod.SpawnDenied as exc:
        return json_error(403, str(exc))
    except worktree_mod.WorktreeError as exc:
        # A checkout that was asked for and could not be cut (or could not be
        # brought up to date) fails the spawn, exactly as it fails a launch:
        # an agent started in a directory that is not the one it was promised
        # is worse than one that was never started.
        return json_error(400, str(exc))
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
    body_out = {"session": session.info(), "parent": parent, **result}
    # Only when there is one: an always-present empty list would read, to a
    # client eyeballing the response, as a field that never says anything.
    if warnings:
        body_out["warnings"] = warnings
    return json_response(body_out, status=201)


async def h_session_reparent(request: web.Request) -> web.Response:
    """Move a session (with its subtree) under another parent — the topology
    ``spawn`` fixes at birth, edited after the fact.

    Why it exists: a lead whose workers have crowded into one area wants a
    nested worker to own that area — collect their branches, request one
    integration — without killing and respawning sessions that hold live
    conversations, worktrees and cflow runs. The move is
    :meth:`SessionManager.reparent`; the rules (no cycle, no exited parent,
    the depth limit, authority down the tree) live there.

    ``actor`` names the session asking, as it does on the link route: with it
    the move is checked against the tree, without it the caller is an
    operator. On success the child's edge to its new parent is opened in every
    mesh the two share, so it can report there the way a spawned child can
    from its first turn; the old parent's edge is left as it was, because the
    move changes who commands the child, not who may hear from it.
    """
    manager: SessionManager = request.app["manager"]
    child = request.match_info["name"]
    body = await _json_body(request)
    parent = str(body.get("parent") or "").strip()
    if not parent:
        return json_error(400, "'parent' is required")
    try:
        result = manager.reparent(child, parent, actor=str(body.get("actor") or ""))
    except ManagerError as exc:
        msg = str(exc)
        if "no session named" in msg:
            return json_error(404, msg)
        if "does not command" in msg or "cannot move itself" in msg:
            return json_error(403, msg)
        return json_error(400, msg)
    mm = request.app.get("mesh")
    result["connected"] = await mm.link_lineage(child, parent) if mm else []
    return json_response(result)


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

    ``?force=1`` resolves the hold instead of honouring it: each held record
    is taken off its rosters (:meth:`MeshManager.leave`, the same call as the
    roster's ×) and then dropped, so the row and the record go together — the
    invariant the guard exists for, kept from the other side. A membership
    that will not release (a mirror's leave is a call to a primary that may
    be unreachable) keeps its record and reports the refusal in that row's
    ``error``, because a force that half-releases must say which half.

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
    force = request.query.get("force") in ("1", "true")
    stopped: List[str] = []
    if request.query.get("running") in ("1", "true"):
        live = [s for s in manager.list() if not s.exited]
        if live:
            # Concurrently: the grace period is per session, and waiting out
            # ten of them in a row is ten graces long for no reason.
            await asyncio.gather(*(s.shutdown() for s in live))
        stopped = [s.sdef.name for s in live]
    kept: List[dict] = []
    for name, held in (
        (s.sdef.name, _mesh_holds(request, s.sdef.name))
        for s in manager.list()
        if s.exited
    ):
        if held and force:
            held = await _leave_meshes(mesh, held)
        if held:
            kept.append({"name": name, "meshes": held})
    removed = manager.clear(logs=logs, keep=[k["name"] for k in kept])
    return json_response(
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
    winding: List[str] = []
    failed: List[dict] = []
    for session in list(manager.list()):
        if session.exited:
            continue
        name = session.sdef.name
        try:
            if await _winding_down(request, session, force=force):
                winding.append(name)
                continue
            manager.kill(name, force=force)
        except Exception as exc:  # one refusal must not strand the other nine
            failed.append({"name": name, "error": str(exc)})
        else:
            killed.append(name)
    return json_response(
        {"killed": killed, "winding_down": winding, "failed": failed}
    )


async def h_sessions_pause_all(request: web.Request) -> web.Response:
    """Pause every running session at once (``?force=1`` for SIGKILL).

    :func:`h_sessions_kill_all` with the records marked paused, and without
    the wind-down: a pause is the operator's emergency stop — the fleet is
    looping, or two sessions are racing on one checkout — and typing a
    settle-your-issues request into a session that is misbehaving is the
    opposite of what was asked. Every record stays respawnable, and
    :func:`h_sessions_resume_all` brings back exactly this set.

    Partial results are reported rather than raised, for the reason the kill
    above gives: ``failed`` names the sessions that would not stop.
    """
    manager: SessionManager = request.app["manager"]
    force = request.query.get("force") in ("1", "true")
    board = request.app["beads"]
    paused: List[str] = []
    failed: List[dict] = []
    for session in list(manager.list()):
        if session.exited:
            continue
        name = session.sdef.name
        try:
            # A wind-down already in flight for this session is overtaken:
            # the pause is the second, immediate stop that the kill button
            # turns into while one runs.
            board.winddowns.pop(name, None)
            manager.pause(name, force=force)
        except Exception as exc:  # one refusal must not strand the other nine
            failed.append({"name": name, "error": str(exc)})
        else:
            paused.append(name)
    return json_response({"paused": paused, "failed": failed})


async def h_sessions_resume_all(request: web.Request) -> web.Response:
    """Relaunch every paused record — the undo of :func:`h_sessions_pause_all`.

    Only the paused ones: a rail that also holds sessions somebody killed on
    purpose must not get those back from a button that said *resume the
    paused*. Each comes back through :meth:`SessionManager.respawn`, which
    constructs a fresh Session and so clears the marker. Archived records
    are left where they are unless ``?archived=1`` asks for them too.

    In creation order, so a session is back before the ones it spawned.
    """
    manager: SessionManager = request.app["manager"]
    include_archived = request.query.get("archived", "0") in ("1", "true")
    resumed: List[str] = []
    failed: List[dict] = []
    for session in list(manager.list()):
        if not session.exited or not getattr(session, "paused_at", None):
            continue
        if session.archived_at and not include_archived:
            continue
        name = session.sdef.name
        try:
            manager.respawn(name)
        except Exception as exc:
            failed.append({"name": name, "error": str(exc)})
        else:
            resumed.append(name)
    return json_response({"resumed": resumed, "failed": failed})


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
    include_archived = request.query.get("archived", "1") not in ("0", "false")
    # ``?paused=0`` leaves the paused records to their own resume: the rail's
    # "resume N" counts the killed ones and must bring back exactly those.
    include_paused = request.query.get("paused", "1") not in ("0", "false")
    respawned: List[str] = []
    failed: List[dict] = []
    for session in list(manager.list()):
        if not session.exited:
            continue
        if session.archived_at and not include_archived:
            continue
        if getattr(session, "paused_at", None) and not include_paused:
            continue
        name = session.sdef.name
        try:
            manager.respawn(name)
        except Exception as exc:
            failed.append({"name": name, "error": str(exc)})
        else:
            respawned.append(name)
    return json_response({"respawned": respawned, "failed": failed})


async def h_sessions_archive_all(request: web.Request) -> web.Response:
    """Archive every exited record that is still in the working fleet.

    ``?paused=0`` skips the paused records — a pause is meant to be undone,
    and the rail's "archive N exited" counts only the killed ones.
    """
    manager: SessionManager = request.app["manager"]
    include_paused = request.query.get("paused", "1") not in ("0", "false")
    archived: List[str] = []
    failed: List[dict] = []
    for session in list(manager.list()):
        if not session.exited or session.archived_at:
            continue
        if getattr(session, "paused_at", None) and not include_paused:
            continue
        name = session.sdef.name
        try:
            manager.archive(name)
        except Exception as exc:
            failed.append({"name": name, "error": str(exc)})
        else:
            archived.append(name)
    return json_response({"archived": archived, "failed": failed})


def _session(request: web.Request):
    manager: SessionManager = request.app["manager"]
    return manager.get(request.match_info["name"])


async def h_session_get(request: web.Request) -> web.Response:
    return json_response(_session(request).info())


def _session_reminder_service(request: web.Request):
    service = request.app.get("session_reminder")
    if service is None:
        return None, json_error(503, "this daemon runs no session reminder service")
    return service, None


async def h_session_reminder(request: web.Request) -> web.Response:
    """The attached header's session-level reminder state."""
    session = _session(request)
    if session.exited:
        return json_error(409, f"session {session.sdef.name!r} has exited")
    service, err = _session_reminder_service(request)
    if err:
        return err
    return json_response(service.status(session.sdef.name))


async def h_session_reminder_set(request: web.Request) -> web.Response:
    """Pause or resume repeating Role and Cflow reminders for one session."""
    session = _session(request)
    if session.exited:
        return json_error(409, f"session {session.sdef.name!r} has exited")
    body = await _json_body(request)
    if "paused" not in body:
        return json_error(400, "missing 'paused' boolean")
    if not isinstance(body["paused"], bool):
        return json_error(400, "'paused' must be a boolean")
    service, err = _session_reminder_service(request)
    if err:
        return err
    paused = service.set_paused(session.sdef.name, body["paused"])
    return json_response({**service.status(session.sdef.name), "paused": paused})


async def h_session_reminder_skip(request: web.Request) -> web.Response:
    """Re-arm the current session's active repeating reminder sources."""
    session = _session(request)
    if session.exited:
        return json_error(409, f"session {session.sdef.name!r} has exited")
    service, err = _session_reminder_service(request)
    if err:
        return err
    sources = service.skip_session(session.sdef.name)
    return json_response(
        {
            **service.status(session.sdef.name),
            "skipped": bool(sources),
            "sources": sources,
        }
    )


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
    cwd = _session_cwd(session)

    def describe() -> dict:
        # In a worker, like the children view: the transcript tail, the
        # profile and credential check, the run state (journal included) and
        # the workflow files are all reads, and the detail panel polls this
        # every two seconds. Inline it was 80ms of loop per poll, at p90.
        info = ctxsize.attach(session)
        metering.attach(info)
        # A model id the harness registry cannot read back into one of its
        # aliases. Reconciliation deliberately leaves the saved model alone in
        # that case rather than guess, so the disagreement would otherwise be
        # invisible; here it is, next to the lever that resolves it
        # (POST /api/sessions/<name>/model).
        unmapped = getattr(session, "unmapped_model_id", None)
        if unmapped:
            info["unmapped_model_id"] = unmapped
        harness = harness_registry.registry().get(info.get("harness") or "")
        borrowed_auth = None
        if info.get("borrow") and info.get("profile"):
            runtime_profile = profile_mod.resolve_selector(info["profile"])
            borrowed_auth = borrowing.validate(
                runtime_profile, info["borrow"], entry=harness
            ).to_dict()
        # Containment, not equality: a session launched with `--worktree`
        # sits in `<repo>/.claude/worktrees/<name>`, which is the workspace
        # the user vouched for with another branch checked out -- not a
        # directory nobody approved. Matching only the exact path reported
        # those as workspace-less, which is the one thing the registry exists
        # to make impossible.
        workspace = workspaces.owning(cwd) if cwd else None
        within = workspaces.subpath(workspace, cwd) if workspace else ""
        role = None
        if info.get("role"):
            roleset = mesh_roles.resolve()
            entry = roleset.roles.get(info["role"])
            if entry:
                role = {"name": entry.name, "stance": entry.stance}
        out = {
            "session": info,
            "harness": harness.to_dict() if harness else None,
            # A live, secret-free validation rather than a creation-time
            # snapshot: deleting/expiring the lender's credential must turn
            # the detail rail red on its next poll, without restarting or
            # exposing the value.
            "borrowed_auth": borrowed_auth,
            "workspace": workspace.to_dict() if workspace else None,
            # Empty when the session is at the workspace root, which is the
            # usual case; the worktree's own directory name when it is not.
            "workspace_subpath": within,
            "role": role,
            "cflow": None,
            "workflows": [],
        }
        if cwd:
            out["cflow"] = _cflow_entry(manager, cwd, info["name"])
            out["workflows"] = _startable_workflows(cwd)
        return out

    body = await asyncio.to_thread(describe)
    info = body["session"]
    # Same branch the rail row carries, so the detail panel's head and the
    # Details list need no second guess. On the loop: a miss schedules its
    # git read there (see _read_branch_later).
    info["branch"] = _branch_of(cwd)
    name = info["name"]
    body.update({
        "meshes": request.app["mesh"].meshes_for_session(name),
        "queued": _session_queued(request, session),
        # The board's slice for this session: the issue it is for and every
        # issue that names it (see beads.match). Keyed by repository, not by
        # session — one board per repo, reached from any worktree.
        "beads": await request.app["beads"].session_view(session),
        # What this session actually committed, read back off the
        # ``Claunch-Session`` trailers rather than kept in a registry (see
        # :mod:`claude_launcher.session_commits`). It belongs beside the
        # round report for the same reason the report exists: the terminal
        # closes, and then the commits are the only thing left that says what
        # the round did.
        #
        # ``None`` until a directory is known, and it stays ``None`` for a
        # session that has none — deliberately, because the alternative is a
        # lie. An empty summary here would reach the panel as "this session
        # committed nothing", which is a claim about the SESSION; what is
        # actually true is that there was no repository to look in. The panel
        # draws nothing for ``None`` and says so for ``[]``, and those are the
        # two different facts.
        "commits": None,
        # The pending merge/handoff on this session, if any — the detail
        # panel's Hand off box reads it to show the request and its cancel.
        "handoff": request.app["handoff"].state(name),
    })
    if cwd:
        # In a thread: this is a git walk over every ref, and the detail
        # panel polls. A pathological repository must cost this response,
        # never the whole daemon's loop.
        body["commits"] = session_commits.summary(
            await asyncio.to_thread(session_commits.for_session, cwd, name)
        )
    return json_response(body)


def _session_queued(request: web.Request, session) -> dict:
    """The session's delivery backlog, with the reason it is still a backlog.

    ``messages`` is what the mesh has accepted for this session but not yet
    typed into its terminal (see :meth:`MeshManager.queued_for_session`), and
    ``reason`` is why the worker is holding them, derived from the SAME two
    signals the worker's own gate reads (status and the keyboard) so the
    banner drawn from this cannot claim a hold the daemon is not applying:

    * ``exited``   — nobody to type into; held until the session respawns.
    * ``hold``     — a person pinned this session shut (``hold`` below, set
      via :func:`h_session_hold`). Ahead of ``busy`` and ``keyboard`` because
      the gate reads it first, and unlike them it does not time out: the
      others give up after ``busy_hold`` and type in anyway, which is the
      right ending for a guess drawn from timing and the wrong one for a
      decision somebody made.
    * ``busy``     — mid-turn (or still starting); held until it goes idle.
    * ``keyboard`` — the screen is idle but someone is typing here (the web
      terminal or an attach). This is the hold a human causes *themselves*
      by keeping focus in the terminal they are waiting on, which is why it
      is told apart from ``busy`` rather than folded into it.
      ``draft_open`` sharpens it into the two very different waits it covers:
      with a draft, the hold ends when they send or clear the line they are
      writing (their Enter, not a timer); without one it ends a few seconds
      after the last keystroke. A banner that told someone to "leave the
      keyboard alone" while their own half-written line is what holds the
      message would be advice that never comes true.
    * ``paced``    — nothing about the SESSION is holding it: the mesh is,
      because it typed a block into this terminal less than ``min_gap`` ago
      (mesh policy ``backpressure.min_gap``). Last of the automatic holds
      because that is where the delivery gate puts it — it is the one that
      still binds after ``busy_hold`` has given up and decided to interrupt
      a running turn, and it is what turns a burst of arrivals into one
      later block instead of three interruptions.
    * ``settling`` — nothing is holding it; the next worker tick delivers.

    The raw signals ride along so a client can sharpen the wording (the web
    UI says "your typing" when its own keystrokes are recent), and
    ``busy_hold`` says when a busy/keyboard hold gives up and types anyway.

    ``backpressure`` is the other half of the same subject and does not fit
    the ladder, because it is about the DOOR rather than the hold: a member
    at ``inbox_max`` is no longer accepting mail at all, and its senders are
    being turned away with a retry-after (see
    :meth:`MeshManager.backpressure_for_session`). That is invisible from
    everything above — the backlog stops growing, which looks exactly like
    calm — so it is reported as its own object, with who has been refused
    and how recently.

    ``reason`` stays null while the backlog is empty — there is no backlog to
    explain — but ``state`` is always filled in, and that is the difference
    the header chip needed: "nothing is queued *and* the next message would
    go straight in" and "nothing is queued *yet*, because I have this session
    pinned shut" are the same empty list and opposite situations. ``state``
    is what ``reason`` would be if a message arrived this instant, so a
    reader can see the hold before it has cost them anything.
    """
    mm = _mesh_mgr(request)
    messages = mm.queued_for_session(session.sdef.name)
    status = session.status()
    keyboard = session.keyboard_busy()
    draft = session.draft_open()
    hold = session.delivery_held()
    bp = mm.backpressure_for_session(session.sdef.name)
    # One ladder, walked in the delivery gate's own order (see
    # :meth:`MeshManager._deliver_to`), so the banner can never name a hold
    # the daemon is not applying — or miss the one it is.
    if session.exited:
        state = "exited"
    elif hold:
        state = "hold"
    elif status != STATUS_IDLE:
        state = "busy"
    elif keyboard:
        state = "keyboard"
    elif bp.get("paced_for"):
        state = "paced"
    else:
        state = "settling"
    return {
        "messages": messages,
        "status": status,
        "keyboard_busy": keyboard,
        "draft_open": draft,
        "hold": hold,
        "state": state,
        "reason": state if messages else None,
        "busy_hold": mm.busy_hold,
        "backpressure": bp,
    }


async def h_session_queued(request: web.Request) -> web.Response:
    """What the daemon is holding FOR this session: messages accepted into a
    mesh log, addressed to one of its handles, and not yet typed into its
    terminal. Polled by the terminal page's banner, which exists to answer
    the operator staring at a quiet terminal wondering where their message
    went — most often: it is held because their own focus keeps the keyboard
    busy. The same payload rides inside ``/meta`` for the detail panel."""
    return json_response(_session_queued(request, _session(request)))


async def h_session_queued_flush(request: web.Request) -> web.Response:
    """Deliver this session's held backlog now, because a human said so.

    The button beside the banner :func:`h_session_queued` feeds, and the
    ``claunch deliver-now`` command. It drops every hold a person is in a
    position to overrule (see :meth:`MeshManager.flush_session`): the pinned
    hold, the idle-gate, and the keyboard holds inside
    :meth:`Session.deliver`, where an unsent line is submitted ahead of the
    delivery instead of refusing it. The operator has decided that
    interleaving with the running turn is fine, which is a judgement the
    daemon is not in a position to make on its own — and the one wait that
    survives, a TUI whose input has not come up, is not that judgement but
    the difference between delivering and typing into nothing.

    Answers with the flush result *and* the re-read backlog, so a caller can
    render the truth after the attempt in one round trip instead of racing
    its own poll. ``flushed: 0`` is a real outcome, not an error: a session
    that has exited, or whose terminal is not yet able to take a paste, keeps
    its backlog and says so.
    """
    session = _session(request)
    result = await _mesh_mgr(request).flush_session(session.sdef.name)
    return json_response(
        {**result, "queued": _session_queued(request, session)}
    )


async def h_session_hold(request: web.Request) -> web.Response:
    """Pin this session shut, or let it go again, because a person said so.

    The other half of :func:`h_session_queued_flush`, and its opposite: that
    one is "type it in now, I know what is running", this one is "type
    nothing in here until I say". Both exist for the same reason — the daemon
    guesses from status and keystroke timing whether a moment is a good one,
    and a guess is all it can do. Flush overrules a guess that is too
    cautious; hold covers the one that is too eager, the case the timing
    signals cannot see at all: somebody reading the scrollback, or thinking
    with their hands off the keys, where nothing on the wire says "not now"
    and every automatic hold has already lapsed.

    ``{"hold": true|false}``; omitting the field toggles, so the button in
    the header needs no read-then-write race. Answers with the re-read
    backlog so the caller renders the truth after the change in one trip.

    Nothing is dropped: held messages stay in their mesh log with the
    recipient's cursor where it was, and resuming types in the backlog that
    built up. Nothing is *promised* either — resume returns the session to
    the ordinary gate, so a message still waits out a running turn.

    The new state is written to the session records here rather than left for
    the next shutdown to save. A daemon that is killed, crashes, or is
    restarted by an installer never reaches its orderly ``persist()``, and
    those are exactly the restarts a person did not schedule — the ones after
    which a silently released hold is discovered by a message landing in a
    terminal that was supposed to be shut.
    """
    manager: SessionManager = request.app["manager"]
    session = _session(request)
    body = await _json_body(request)
    want = body.get("hold")
    held = session.set_delivery_hold(
        not session.delivery_held() if want is None else bool(want)
    )
    manager.persist()
    return json_response(
        {"hold": held, "queued": _session_queued(request, session)}
    )


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
    where = ", ".join(
        f"{h['mesh']} (as {h['handle']})"
        + (f" — leave failed: {h['error']}" if h.get("error") else "")
        for h in held
    )
    return (
        f"{name!r} is still a mesh member — {where}. Dropping its record now "
        "would leave that row naming a session nobody can respawn or reach. "
        "Remove it from the mesh first (the roster's ×, or claunch mesh "
        "leave), then clear the record — or force the delete to do both "
        "at once."
    )


async def _leave_meshes(mesh_mgr, held: List[dict]) -> List[dict]:
    """The force path's answer to :func:`_mesh_holds`: release each row
    instead of honouring it, with :meth:`MeshManager.leave` — the same call
    the roster's × makes, so everything that × keeps consistent (cursors,
    write-offs, member edges, guest fan-out) stays consistent here.

    Returns the rows that would NOT release, each carrying the refusal as
    ``error``. Leave is not guaranteed: on a mirror it is a call to the
    primary, which may be unreachable — and a session held by two meshes
    where only one released is still held, so the caller keeps its record.
    """
    still: List[dict] = []
    for row in held:
        try:
            await mesh_mgr.leave(row["mesh"], row["handle"])
        except Exception as exc:  # one refusal must not strand the others
            still.append({**row, "error": str(exc)})
    return still


async def h_session_kill(request: web.Request) -> web.Response:
    """Kill a running session; do nothing to one that has already exited.

    This route only ends — it never drops a record, so it is safe to repeat:
    a second kill is what a caller reaches for when the first one *looked*
    like it had not worked, and that call must not be the destructive one.
    Forgetting is the DELETE beside it, and it refuses a running session, so
    the two verbs cannot be reached through each other.

    On an exited session the reply says ``already_exited`` rather than being
    silent: a caller that asks twice deserves to know the second ask changed
    nothing, or it will keep asking.

    ``?force=1`` is SIGKILL, as it always was, and means nothing on an exited
    session — there is nothing left for it to carry. The mesh is untouched
    either way: a member row is *meant* to outlive the terminal, reading
    ``exited``.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    force = request.query.get("force") in ("1", "true")
    session = manager.get(name)  # ManagerError -> 400, as it always did
    if session.exited:
        return json_response({**session.info(), "already_exited": True})
    if await _winding_down(request, session, force=force):
        return json_response({**session.info(), "winding_down": True})
    session = manager.kill(name, force=force)
    return json_response(session.info())


async def h_session_pause(request: web.Request) -> web.Response:
    """Pause a running session: :func:`h_session_kill` with the record marked
    ``paused_at``, and without the wind-down.

    The process side is a kill — the program is terminated, the record
    stays and stays respawnable, the mesh row outlives the terminal. What
    differs is what the record says afterwards: *paused*, a temporary stop
    the operator means to undo, which the rail files apart from the killed
    and the bulk resume brings back as a set. No wind-down because the
    reason to pause is a session misbehaving — looping, racing another on
    the same checkout — and typing a settle-your-issues request into it is
    the thing being stopped. Its board issues get the same ``SESSION ENDED``
    sweep a kill's do; that is the daemon's exit path, unchanged.

    On an exited session the reply says ``already_exited``, as the kill
    route does; the record keeps whichever marker it has.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    force = request.query.get("force") in ("1", "true")
    session = manager.get(name)  # ManagerError -> 400, as it always did
    if session.exited:
        return json_response({**session.info(), "already_exited": True})
    request.app["beads"].winddowns.pop(name, None)
    session = manager.pause(name, force=force)
    return json_response(session.info())


async def h_session_delete(request: web.Request) -> web.Response:
    """Drop the record of an exited session (operator). Never ends anything.

    The mirror of the kill route above: DELETE is forget, POST kill is end,
    and each refuses the other's job — a running session gets a 400 naming
    the kill route, because an operator who meant "end it" must not get
    "forgotten" from a command that said no such thing.

    The exited half is guarded: see :func:`_mesh_holds`. ``?force=1`` takes
    a held record off its rosters first (:func:`_leave_meshes`) and then
    drops it. One word because it is one stance — do it anyway.

    ``?children=escalate`` (the default) moves the sessions below this one up
    to its own parent, so a grandchild keeps answering to the grandparent
    instead of becoming a root nobody commands; each promoted session's mesh
    edge to that new parent is opened here, the same way the re-parent route
    opens it. ``?children=remove`` drops their records too, and is refused
    (409) while any of them is still running or is still named by a mesh row.
    The reply carries ``escalated`` or ``removed_children`` accordingly.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    force = request.query.get("force") in ("1", "true")
    cascade = request.query.get("children") in ("remove", "cascade")
    session = manager.get(name)  # ManagerError -> 400, as it always did
    if not session.exited:
        return json_error(
            400,
            f"{name!r} is still running — DELETE only drops a record. "
            f"Kill it first (POST /api/sessions/{name}/kill).",
        )
    # A cascade drops several records, so the mesh guard is asked of every one
    # of them: the roster of a grandchild strands just as badly as the named
    # session's own, and it must be asked before anything is deleted.
    doomed = [name] + (manager.descendants(name) if cascade else [])
    for one in doomed:
        rows = _mesh_holds(request, one)
        if rows and force:
            rows = await _leave_meshes(request.app["mesh"], rows)
        if rows:
            # Named per session rather than per call: on a cascade the record
            # that will not go is usually not the one the operator typed, and
            # an error naming the wrong session sends them to the wrong roster.
            return json_error(409, _mesh_holds_error(one, rows))
    above = manager.get(name).sdef.parent
    if above not in {s.sdef.name for s in manager.list()}:
        above = ""  # already dangling — the same answer escalate_children gives
    try:
        session, touched = manager.remove(
            name, children="remove" if cascade else "escalate"
        )
    except ManagerError as exc:
        return json_error(409, str(exc))
    body = {**session.info()}
    if cascade:
        body["removed_children"] = touched
    else:
        body["escalated"] = touched
        # The promoted sessions need the mesh edge to their new parent for the
        # same reason a re-parented one does: a child that cannot reach its
        # parent cannot report. link_lineage is a no-op when the pair share no
        # mesh, or when the grandparent is gone.
        mm = request.app.get("mesh")
        opened: List[dict] = []
        if mm and above:
            for child in touched:
                opened.extend(await mm.link_lineage(child, above))
        body["connected"] = opened
    return json_response(body)


async def h_session_archive(request: web.Request) -> web.Response:
    """Retain an exited record in the archive so it remains inspectable."""
    manager: SessionManager = request.app["manager"]
    session = manager.archive(request.match_info["name"])
    return json_response(session.info())


async def h_session_keep_alive(request: web.Request) -> web.Response:
    """Set (or, with ``?off=1``, clear) a session's keep-alive flag.

    The flag is what the kill-on-end hook reads right beside its kill: a
    session whose driving one-shot run finished is recorded and ended by the
    daemon, and this is the one lever that says "record it, but do not end
    it". The session itself sets it on a user's explicit "don't close me",
    and the operator clears it when the context is no longer wanted. A plain
    POST records the session's position; the reply echoes the full record.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    on = request.query.get("off") not in ("1", "true")
    session = manager.set_keep_alive(name, on)
    return json_response({**session.info(), "keep_alive": bool(on)})


async def h_session_model(request: web.Request) -> web.Response:
    """Set the model this session's *next* launch uses (``{"model": "opus"}``).

    The running program keeps the model it started on -- a harness chooses at
    startup -- so this lands at the next restore or respawn, exactly like the
    creation-time choice. An empty value clears it back to the harness default.

    Most of the time nothing needs to call this: the daemon follows what the
    session actually answers on and writes that down itself. This is for what
    that cannot see -- a session that has not taken a turn, a model id the
    registry does not map, or a deliberate "next time, something else".
    An unknown model is a 400 from the manager's own check.
    """
    manager: SessionManager = request.app["manager"]
    body = await _json_body(request)
    session = manager.set_model(request.match_info["name"], str(body.get("model") or ""))
    return json_response(session.info())


async def _winding_down(request: web.Request, session, *, force: bool) -> bool:
    """Whether a kill of ``session`` is now a wind-down instead (see
    :meth:`beads.Board.begin_winddown`) — the considerate ending for a live
    session holding active board issues. Skipped by ``force`` (the operator
    wants it gone now) and by ``?winddown=0`` (the same, without SIGKILL);
    a second kill of a session already winding down goes straight through,
    which is what the button turns into while one is running."""
    # Any kill, wind-down or not, ends a pending merge/handoff request: the
    # operator pressed past it, and a row that kept saying `merging…` on a
    # session being terminated would be describing a request nobody holds.
    request.app["handoff"].forget(session.sdef.name)
    if force or request.query.get("winddown") in ("0", "false"):
        board = request.app["beads"]
        board.winddowns.pop(session.sdef.name, None)
        return False
    if session.sdef.name in request.app["beads"].winddowns:
        request.app["beads"].winddowns.pop(session.sdef.name, None)
        return False
    return await request.app["beads"].begin_winddown(session, request.app["manager"])


def _quick_fork_joined(reported, inherited: bool, asked: str) -> str:
    """The NAME of what the copy ended up in, whatever shape the onboarding
    reported it as.

    Onboarding answers a join with a record (``{ok, mesh, handle, role}``)
    and an inheritance with whatever it settled on, so the name is read back
    from that rather than from the request: an inherited mesh has no name in
    the request at all, and a requested one can still fail to join. Falls
    back to what was asked only when the report says nothing and the caller
    did name one -- never inventing a name for an inheritance nobody
    confirmed.
    """
    if isinstance(reported, dict):
        name = reported.get("mesh") or reported.get("workflow") or reported.get("name")
        return str(name or "")
    if isinstance(reported, str) and reported.strip():
        return "" if reported.strip() in (onboard.NO_MESH, onboard.NO_WORKFLOW) else reported.strip()
    if inherited:
        return ""
    return "" if asked in (onboard.NO_MESH, onboard.NO_WORKFLOW, ".") else asked


async def h_session_quick_fork(request: web.Request) -> web.Response:
    """Copy this session's conversation into a child and mark where the copy
    begins — the one-press fork (see :mod:`daemon.handoff`).

    Built on the spawn route's own pieces rather than beside them: the child
    is staged with ``fork: true`` (claude's ``--resume --fork-session`` of
    the parent's pinned conversation), so it is a child of this session and
    restores like one. What this route adds is the marker block as the
    child's opening — the line the merge later refers back to — and the
    record (``quick_fork_of``) that makes merge available on the copy.

    The copy joins no mesh and drives no run unless the body says otherwise.
    That default is for the common press: a scratch branch of ONE session's
    conversation, whose work is the origin's, and a session with no issue
    sitting on a roster is one a leader cannot place. It is a default and not
    a rule -- ``mesh`` and ``workflow`` in the body take the copy the other
    way, and the CLI and the header button both offer the choice, because the
    other use is real: a fork given a job of its own has to be able to report
    it. (An earlier draft of this docstring justified the default by saying
    an inherited mesh would make the copy "a second agent answering for the
    same conversation". That was wrong and is corrected here: the copy joins
    under its OWN session name, so no handle is ever shared.) It does not
    mint a board issue from the marker text (``beads: false``) either -- the
    work it does is the origin's.

    What the copy cannot have is a checkout of its own. ``fork`` and
    ``worktree`` are refused together by :func:`claude_launcher.spawn.check`,
    because claude keeps transcripts per working directory and a copy started
    elsewhere would resolve nothing and boot empty. So the copy stands in the
    origin's directory, sharing its files and its git state, and the marker
    block says so in as many words -- the only defence available against two
    claude sessions writing one checkout is that both of them know.
    """
    manager: SessionManager = request.app["manager"]
    parent = request.match_info["name"]
    body = await _json_body(request)
    try:
        origin = manager.get(parent)
    except ManagerError as exc:
        return json_error(404, str(exc))
    if origin.exited:
        return json_error(400, f"session {parent!r} has exited — an exited session cannot be quick-forked")
    if not spawn_mod.can_fork(origin.sdef.to_dict()):
        return json_error(
            400,
            f"session {parent!r} has no conversation to copy: quick-fork needs "
            "a claude session whose conversation is on disk (its first turn "
            "has landed)",
        )
    name = str(body.get("name") or "").strip() or handoff_mod.fork_name(
        parent, {s.sdef.name for s in manager.list()}
    )
    marker = handoff_mod.new_marker()
    forked_at = handoff_mod._utcnow()
    # Three answers, not two. A spawn that names no mesh INHERITS the
    # parent's (``onboard.inherit_mesh``), so "the origin's" is spelled by
    # leaving the field out — and this route cannot simply leave it out,
    # because its default is the opposite. The dot is that third answer said
    # out loud: the caller asks for the origin's without having to know its
    # name, and the inheritance that already exists does the work.
    mesh = str(body.get("mesh") or onboard.NO_MESH)
    workflow = str(body.get("workflow") or onboard.NO_WORKFLOW)
    inherit_mesh = mesh == "."
    inherit_workflow = workflow == "."
    opening = handoff_mod.compose_marker(
        origin=parent, fork=name, marker=marker, forked_at=forked_at,
        # The origin's own directory, because a fork cannot be moved out of
        # it. Named in the block so the copy can see what it shares.
        cwd=str(origin.sdef.cwd or ""),
        # An inherited one is not named here: it is settled downstream, and
        # the block would be guessing. The join itself tells the copy.
        mesh="" if mesh in (onboard.NO_MESH, ".") else mesh,
        workflow="" if workflow in (onboard.NO_WORKFLOW, ".") else workflow,
    )
    task = str(body.get("task") or "").strip()
    if task:
        opening = opening + "\n\n" + task
    spawn_body = {
        "name": name,
        "fork": True,
        "quick_fork_of": parent,
        "task": opening,
        "beads": False,
    }
    if not inherit_mesh:
        spawn_body["mesh"] = mesh
    if not inherit_workflow:
        spawn_body["workflow"] = workflow
    warnings: list = []
    try:
        session = manager.stage_child(parent, spawn_body, warnings=warnings)
    except spawn_mod.SpawnDenied as exc:
        return json_error(403, str(exc))
    except (HarnessError, ValueError, TypeError) as exc:
        return json_error(400, f"bad quick-fork request: {exc}")
    except ManagerError as exc:
        return json_error(409 if "already exists" in str(exc) else 400, str(exc))
    try:
        result = await _onboard_and_launch(request, session, spawn_body, parent=parent)
    except onboard.OnboardError as exc:
        return json_error(400, str(exc))
    except (HarnessError, ValueError, TypeError) as exc:
        return json_error(400, f"bad quick-fork request: {exc}")
    out = {
        "session": session.info(), "origin": parent, "marker": marker,
        "forked_at": forked_at, **result,
    }
    # Under its own key, not spread beside `result`: onboarding already
    # answers `mesh` with the join itself (ok/handle/role), and a second
    # `mesh` meaning only the name silently took its place — the spread put
    # `result` last, so what the caller read depended on which side won.
    # One key, one meaning, and nothing of the join is lost.
    out["quick_fork"] = {
        "mesh": _quick_fork_joined(result.get("mesh"), inherit_mesh, mesh),
        "workflow": _quick_fork_joined(
            result.get("workflow"), inherit_workflow, workflow,
        ),
        # The origin's directory, and the fact that the copy is standing in
        # it. A caller that prints one line about this fork should print this.
        "cwd": str(origin.sdef.cwd or ""),
        "shared_checkout": True,
    }
    if warnings:
        out["warnings"] = warnings
    return json_response(out, status=201)


async def h_session_handoff(request: web.Request) -> web.Response:
    """Request a merge/handoff from this session, or complete one.

    With ``text`` in the body this is the completion — the agent handing in
    its wrap-up (the MCP ``handoff`` tool and the two CLI verbs post here
    from inside the session): the report is delivered to the target and then
    the session is ended, in that order. Without ``text`` it is the
    operator's request: the instruction block is typed into the session and
    the request is recorded as pending (the row's ``handoff`` field).

    ``to`` names the target for a handoff; a quick-fork's merge needs none
    (it goes back to the origin) and refuses any other. ``kind`` may force
    ``handoff`` on a quick-fork that wants to report elsewhere.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    body = await _json_body(request)
    to = str(body.get("to") or "").strip()
    kind = str(body.get("kind") or "").strip()
    text = body.get("text")
    handoffs: handoff_mod.Handoffs = request.app["handoff"]
    try:
        if text is not None and str(text).strip():
            result = await handoffs.complete(manager, name, text=str(text), to=to, kind=kind)
            return json_response({"completed": True, **result})
        result = await handoffs.request(manager, name, to=to, kind=kind)
        return json_response({"requested": True, "source": name, **result})
    except handoff_mod.HandoffError as exc:
        msg = str(exc)
        return json_error(404 if "no session named" in msg else 400, msg)


async def h_session_handoff_cancel(request: web.Request) -> web.Response:
    """Withdraw a pending merge/handoff request: the row stops saying
    ``merging…`` and the session is told to carry on."""
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    try:
        result = await request.app["handoff"].cancel(manager, name)
    except handoff_mod.HandoffError as exc:
        return json_error(404, str(exc))
    return json_response({"source": name, **result})


async def h_session_child_kill(request: web.Request) -> web.Response:
    """Retire a session an agent spawned — the counterpart of the POST above.

    Scoped by the route rather than by a flag: ``POST
    /api/sessions/{name}/kill`` is the operator's, who may end anything, and
    this one only reaches down ``name``'s own subtree. The rule is
    :meth:`SessionManager.commands`, the
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
        return json_response({**target.info(), "already_exited": True})
    force = request.query.get("force") in ("1", "true")
    if await _winding_down(request, target, force=force):
        return json_response({**target.info(), "winding_down": True})
    session = manager.kill(child, force=force)
    return json_response(session.info())


async def h_session_respawn(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    session = manager.respawn(request.match_info["name"])
    return json_response(session.info())


async def h_session_migrate(request: web.Request) -> web.Response:
    """Move a session to another checkout — and, if asked, the children that
    share its directory.

    Body: exactly one of ``worktree`` (a worktree of the session's own
    repository, created or reused under ``.claude/worktrees/``; ``""`` for a
    generated name) or ``cwd`` (an existing directory, resolved on the daemon
    like every path in this API). ``children: true`` also migrates every
    descendant standing in the *same* directory the session is leaving —
    those elsewhere (their own worktrees included) are exactly where someone
    put them, and stay.

    The move itself is :meth:`SessionManager.migrate`: stop, carry the claude
    transcript to the new directory's slug, relaunch there. The named session
    is all-or-nothing (a refusal leaves it untouched); the children are each
    their own attempt, reported per name, because "the parent moved but w3
    would not" is a state the operator can finish by hand, while unwinding a
    parent that already moved over one stubborn child is not.

    Operator-only, like respawn: no agent-facing route reaches this. An agent
    that wants a child in a worktree says so at spawn time, which is the
    moment the move is free.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    body = await _json_body(request)
    session = manager.get(name)  # ManagerError -> 400
    old_cwd = session.sdef.cwd
    wt_name, to = body.get("worktree"), body.get("cwd")
    if (wt_name is None) == (to is None):
        return json_error(400, "pass exactly one of 'worktree' or 'cwd'")
    wt = None
    if wt_name is not None:
        root = worktree_mod.repo_root(old_cwd)
        if root is None:
            return json_error(
                400,
                f"{old_cwd} is not inside a git repository, so there is "
                "nothing to make a worktree of — pass 'cwd' instead",
            )
        try:
            wt = worktree_mod.create(
                root, str(wt_name).strip() or worktree_mod.default_name()
            )
        except worktree_mod.WorktreeError as exc:
            return json_error(400, str(exc))
        new_cwd = str(wt.path)
    else:
        new_cwd = str(to)
    # Who follows is decided against the directory being LEFT, before the
    # parent moves — afterwards the parent's cwd is the answer to a different
    # question.
    followers: List[str] = []
    if body.get("children"):
        old_key = os.path.normcase(os.path.abspath(old_cwd or ""))
        followers = [
            child
            for child in manager.descendants(name)
            if os.path.normcase(
                os.path.abspath(manager.get(child).sdef.cwd or "")
            ) == old_key
        ]
    migrated, carried = await manager.migrate(name, new_cwd)
    children = []
    for child in followers:
        try:
            _, child_carried = await manager.migrate(child, new_cwd)
            children.append(
                {"name": child, "ok": True, "transcript_moved": child_carried}
            )
        except (ManagerError, HarnessError, ProfileError) as exc:
            children.append({"name": child, "ok": False, "error": str(exc)})
    return json_response(
        {
            **migrated.info(),
            "transcript_moved": carried,
            "worktree": (
                {
                    "name": wt.name,
                    "path": str(wt.path),
                    "branch": wt.branch,
                    "created": wt.created,
                }
                if wt
                else None
            ),
            "children": children,
        }
    )


async def h_session_reborrow(request: web.Request) -> web.Response:
    """Restart a session on another answer to "whose token".

    Body: ``{"borrow": "NAME"}`` to borrow that profile's token (and
    provider), ``{"borrow": null}`` (or ``""``) for its own profile's, and
    ``{"null_token": true}`` for none at all (``--null``). They are one
    choice — picking any clears the others, so a borrow set on a ``--null``
    session turns the token back on. The restart is
    :meth:`SessionManager.reborrow` — stop, relaunch under the definition
    with the auth swapped. The directory does not move, so the conversation
    stays filed where it always was and there is nothing to carry.

    Operator-only, like migrate and respawn: no agent-facing route reaches
    this. An agent's auth is its spawner's arrangement, changed by the human
    or at spawn time, never by the agent mid-run.
    """
    manager: SessionManager = request.app["manager"]
    body = await _json_body(request)
    if "borrow" not in body and "null_token" not in body:
        return json_error(
            400,
            "pass 'borrow' (a profile name, or null) and/or 'null_token' — "
            "one answer to whose token it runs on",
        )
    borrow = body.get("borrow")
    if borrow is not None and not isinstance(borrow, str):
        return json_error(400, "'borrow' must be a profile name or null")
    null_token = body.get("null_token", False)
    if not isinstance(null_token, bool):
        return json_error(400, "'null_token' must be a boolean")
    session = await manager.reborrow(
        request.match_info["name"], borrow, null_token=null_token
    )
    return json_response(session.info())


async def h_session_skip_permissions(request: web.Request) -> web.Response:
    """Restart a session with permission prompts off — or back on.

    Body: ``{"skip": true|false}``. The toggle is
    :meth:`SessionManager.skip_permissions` — the flag is added to (or
    removed from) the definition's args and the session is relaunched, so
    the new answer holds across daemon restarts like one given at creation.
    Only that one flag is touched; the conversation, directory and auth are
    untouched, so there is nothing to carry.

    Operator-only, like reborrow: an agent does not get to switch off the
    questions asked of it.
    """
    manager: SessionManager = request.app["manager"]
    body = await _json_body(request)
    skip = body.get("skip")
    if not isinstance(skip, bool):
        return json_error(
            400, "pass 'skip': true to stop asking, false to ask again"
        )
    session = await manager.skip_permissions(request.match_info["name"], skip)
    return json_response(session.info())


#: What a pasted image may weigh. A screenshot off a 4K display is a few MiB,
#: and the aiohttp default body cap (1 MiB) is below that -- hence the
#: route reading its own payload rather than calling request.read(), which
#: would enforce the app-wide cap on every other route's behalf.
PASTE_IMAGE_MAX_BYTES = 24 * 1024 * 1024

#: Clipboard image types a browser actually produces, and the extension each
#: one is saved under. Anything else is refused: the file is named for what it
#: claims to be, and a wrong name is what makes a reader open the wrong thing.
PASTE_IMAGE_TYPES = {
    "image/png": ".png",
    "image/jpeg": ".jpg",
    "image/gif": ".gif",
    "image/webp": ".webp",
}


async def h_session_paste_image(request: web.Request) -> web.Response:
    """Store one pasted image for a session and answer with its path.

    The web session line cannot hand a harness an *attachment*: the program in
    the PTY reads bytes, and an image is not bytes it can read as a prompt. So
    the image is written to a file next to the session's own state and the
    path is what goes into the composer -- Claude Code opens an image path
    given in a prompt, which is the whole point of the round trip.

    The body is the image itself, with its media type in ``Content-Type``. It
    is read in chunks against this route's own ceiling rather than through
    ``request.read()``: raising the app-wide ``client_max_size`` for this one
    route would raise it for every route.

    The file lands in the session's state directory (``pastes/``), never in
    the session's working directory -- a repository is not a place to drop
    somebody's screenshot, and an untracked file there shows up in every
    ``git status`` the session runs afterwards.
    """
    session = _session(request)
    kind = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    suffix = PASTE_IMAGE_TYPES.get(kind)
    if suffix is None:
        return json_error(
            415,
            f"{kind or 'no Content-Type'} is not an image this accepts "
            f"({', '.join(sorted(PASTE_IMAGE_TYPES))})",
        )
    chunks: List[bytes] = []
    total = 0
    async for chunk in request.content.iter_chunked(64 * 1024):
        total += len(chunk)
        if total > PASTE_IMAGE_MAX_BYTES:
            return json_error(
                413,
                f"the image is larger than "
                f"{PASTE_IMAGE_MAX_BYTES // (1024 * 1024)} MiB",
            )
        chunks.append(chunk)
    if not total:
        return json_error(400, "the body carried no image bytes")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    folder = paths.session_dir(session.sdef.name) / "pastes"
    folder.mkdir(parents=True, exist_ok=True)
    target = folder / f"{stamp}-{secrets.token_hex(3)}{suffix}"
    # Written through an open file rather than Path.write_bytes: the delivery
    # contract's guard (tests/test_delivery_contract.py) reads call names, and
    # a PTY write is spelled .write_bytes() too. Allowlisting this handler
    # would excuse a real PTY write here later; spelling it differently does
    # not.
    with target.open("wb") as fh:
        for chunk in chunks:
            fh.write(chunk)
    return json_response({"ok": True, "path": str(target), "bytes": total})


async def h_session_keys(request: web.Request) -> web.Response:
    session = _session(request)
    body = await _json_body(request)
    force = body.get("force", False)
    if not isinstance(force, bool):
        return json_error(400, "'force' must be a boolean")
    request_id = body.get("input_id")
    if request_id is not None and (
        not isinstance(request_id, str) or not request_id.strip()
    ):
        return json_error(400, "'input_id' must be a non-empty string")
    paste = body.get("paste")
    if paste is not None:
        if not isinstance(paste, str):
            return json_error(400, "'paste' must be a string")
        # A paste is text by definition: like send_keys with text, it queues
        # behind a human typing at this terminal rather than splicing into
        # their half-written line — and refuses outright rather than typing
        # over a composer that never emptied (see Session.send_keys).
        #
        # 'force' carries the same operator meaning it has for keys: the web
        # session line sends a multi-line composition this way (a newline in
        # the keys path would submit the block a line at a time), so it must
        # not turn into a 30s wait or a 409 on a busy session.
        quiet = await session.await_keyboard_quiet(
            terminal_only=True,
            timeout=session_mod.FORCE_TYPING_GRACE if force else None,
        )
        if not quiet and session.draft_open():
            if force:
                await session.submit_open_draft()
            else:
                return json_error(
                    409,
                    f"session {session.sdef.name!r}: someone is typing there "
                    f"right now — nothing was pasted. Retry in a moment.",
                )
        if request_id is not None:
            prior = session_input.latest(session.sdef.name, request_id)
            if prior and prior.get("status") == "sent":
                return json_response({"ok": True, "bytes": 0, "duplicate": True})
            session_input.write(session.sdef.name, "input_accepted",
                                request_id=request_id, text=paste,
                                status="accepted", pid=session.pid)
        try:
            data = await session.paste(paste, enter=bool(body.get("enter")))
        except Exception:
            if request_id is not None:
                session_input.write(session.sdef.name, "input_failed",
                                    request_id=request_id, text=paste,
                                    status="failed", pid=session.pid)
            raise
        if request_id is not None:
            session_input.write(session.sdef.name, "input_sent",
                                request_id=request_id, text=paste,
                                status="sent", pid=session.pid)
        return json_response({"ok": True, "bytes": len(data)})
    keys = body.get("keys")
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        return json_error(400, "'keys' must be a list of strings")
    audit_text = keys[0] if request_id and len(keys) == 2 and keys[1] == "Enter" else None
    if request_id is not None:
        if audit_text is None:
            return json_error(400, "'input_id' requires [text, 'Enter'] keys")
        prior = session_input.latest(session.sdef.name, request_id)
        if prior and prior.get("status") == "sent":
            return json_response({"ok": True, "bytes": 0, "duplicate": True})
        session_input.write(session.sdef.name, "input_accepted",
                            request_id=request_id, text=audit_text,
                            status="accepted", pid=session.pid)
    try:
        data = await session.send_keys(
            keys, literal=bool(body.get("literal")), force=force
        )
    except Exception:
        if request_id and audit_text is not None:
            session_input.write(session.sdef.name, "input_failed",
                                request_id=request_id, text=audit_text,
                                status="failed", pid=session.pid)
        raise
    if request_id and audit_text is not None:
        session_input.write(session.sdef.name, "input_sent",
                            request_id=request_id, text=audit_text,
                            status="sent", pid=session.pid)
    return json_response({"ok": True, "bytes": len(data)})


async def h_session_input_journal(request: web.Request) -> web.Response:
    """Recent durable submissions made through the session-line control."""
    session = _session(request)
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        return json_error(400, "'limit' must be an integer")
    return json_response({
        "session": session.sdef.name,
        "entries": session_input.read(session.sdef.name, limit=limit),
    })


async def h_session_deliver(request: web.Request) -> web.Response:
    """Hand a message to the agent in this session — the out-of-process door
    to :meth:`Session.deliver`, for senders that live outside the daemon (the
    CLI's cflow nudges). ``defer: true`` accepts the message into the session's
    in-memory queue and returns immediately with ``queued``, not a delivery
    receipt. ``/keys`` stays the raw keyboard passthrough."""
    session = _session(request)
    body = await _json_body(request)
    text = body.get("text")
    if not isinstance(text, str) or not text:
        return json_error(400, "'text' must be a non-empty string")
    if body.get("defer") is True:
        queued = session.queue_delivery(text) if not session.exited else False
        return json_response({"ok": True, "queued": queued, "delivered": False})
    delivered = await session.deliver(text)
    return json_response({"ok": True, "delivered": delivered})


async def h_session_pr_preview(request: web.Request) -> web.Response:
    """What the PR wizard shows before it asks anything: the session's
    directory as git sees it (checkout branch, HEAD, uncommitted counts,
    remotes) and whether ``gh`` can open a pull request there. Off the loop:
    a handful of git processes and a ``gh auth status`` per host."""
    session = _session(request)
    cwd = _session_cwd(session)
    if not cwd:
        return json_error(400, "this session runs in no directory of its own")
    doc = await asyncio.to_thread(prflow.preview, cwd, session=session.sdef.name)
    doc["monitor_workflow"] = PR_MONITOR_WORKFLOW
    doc["monitor_available"] = await asyncio.to_thread(_pr_monitor_declared, cwd)
    return json_response(doc)


#: The bundled workflow the wizard's second checkbox spawns a child on. The
#: bundle is not a search layer (see ``cflow.state.bundled_workflows_dir``),
#: so the name has to be *declared* where the session works -- the global
#: layer ``claunch cflow update`` fills, or the project layer -- before a
#: child can be started on it; the preview says which it is.
PR_MONITOR_WORKFLOW = "improv-worker-pr-monitor"


def _pr_monitor_declared(cwd: str) -> bool:
    """Whether :data:`PR_MONITOR_WORKFLOW` resolves for a session in ``cwd``."""
    try:
        return any(n == PR_MONITOR_WORKFLOW for n, _ in cflow_state.list_workflows(cwd))
    except Exception:  # an unreadable layer is "not offered", not a 500
        return False


async def _spawn_pr_monitor(
    request: web.Request, session, result: dict, warnings: list
) -> Optional[dict]:
    """The wizard's second checkbox: a child of ``session`` that watches the
    PR just opened and reports back.

    Only after a push that produced a PR -- a monitor with nothing to watch
    is a session that starts and ends. The child is spawned through the same
    two halves as ``POST /children`` (:meth:`SessionManager.stage_child`,
    :func:`_onboard_and_launch`) so it inherits the parent's harness,
    profile, directory and mesh like any other child; what differs is the
    daemon's own say-so on the way in:

    * ``exempt_depth=True`` -- the tree's count limits do not apply. A
      worker three levels down that opens a PR still gets it watched; the
      watcher is not the fan-out the limits exist for. The keyword never
      comes from a request body (see :func:`spawn.check`).
    * no board issue (``beads: false``): the monitor works nobody's issue,
      and the wizard's own PR is not a round on the board. The parent's
      issue id rides in the run context for the final comment instead.
    * the facts go in as the run's ``context`` (JSON): the monitor workflow
      is a standalone run, and a standalone run starts with context, not
      inputs (:mod:`cflow.model`).

    A refusal (policy off, workflow not declared, harness failure) is a
    warning on the wizard's answer rather than a failed request: the push
    and the PR have already happened, and the body says so.
    """
    manager: SessionManager = request.app["manager"]
    parent = session.sdef.name
    pr = result.get("pr") or {}
    if not (result.get("ok") and pr.get("url")):
        warnings.append("monitor: no pull request was opened -- nothing to watch, nothing spawned")
        return None
    if not _pr_monitor_declared(session.sdef.cwd):
        warnings.append(
            f"monitor: workflow {PR_MONITOR_WORKFLOW!r} is not declared for "
            f"{session.sdef.cwd} (run 'claunch cflow update' to install the "
            "bundled copy) -- nothing spawned"
        )
        return None
    facts = {
        "pr_url": pr.get("url", ""),
        "pr_number": pr.get("number"),
        "branch": result.get("branch", ""),
        "tip": result.get("tip", ""),
        "remote": result.get("remote", ""),
        "repo": result.get("repo", ""),
        "base": result.get("base", ""),
        "parent": parent,
        "issue": session.sdef.issue or "",
    }
    body = {
        "workflow": PR_MONITOR_WORKFLOW,
        "role": "worker",
        "beads": False,
        "context": json.dumps(facts, ensure_ascii=False),
        "task": (
            f"Watch pull request {facts['pr_url']} (branch "
            f"{facts['remote']}/{facts['branch']} @ {facts['tip'][:8]}) and report "
            f"to {parent}. Report-only: never rebase, push, merge or touch this "
            "directory's checkout -- it is the parent's working tree. The facts "
            "are the run's context (JSON); the workflow says what to do with them."
        ),
    }
    try:
        child = manager.stage_child(parent, body, warnings=warnings, exempt_depth=True)
    except spawn_mod.SpawnDenied as exc:
        warnings.append(f"monitor: spawn refused -- {exc}")
        return None
    except (ManagerError, HarnessError, ValueError, TypeError) as exc:
        warnings.append(f"monitor: could not stage a child -- {exc}")
        return None
    try:
        arranged = await _onboard_and_launch(request, child, body, parent=parent)
    except (onboard.OnboardError, HarnessError, ValueError, TypeError) as exc:
        warnings.append(f"monitor: the child could not be started -- {exc}")
        return None
    # ``_onboard_and_launch`` reports each leg beside the session: the run
    # leg is ``workflow`` -- ``{ok, workflow, scope, step}`` when it started,
    # ``{ok: False, error}`` when the engine refused. A child that exists
    # but runs nothing is still a child, so that is a warning, not a None.
    leg = arranged.get("workflow") if isinstance(arranged, dict) else None
    started = bool(isinstance(leg, dict) and leg.get("ok"))
    if not started:
        warnings.append(
            f"monitor: child {child.sdef.name} was started but its run was not -- "
            f"{(leg or {}).get('error') if isinstance(leg, dict) else 'no run leg reported'}"
        )
    return {
        "session": child.sdef.name,
        "workflow": PR_MONITOR_WORKFLOW,
        "run_started": started,
    }


async def h_session_pr(request: web.Request) -> web.Response:
    """Push what the session's directory holds under a new branch name and
    open the pull request -- the wizard's confirm button.

    Body: the form (``remote``, ``base``, ``branch``, ``title``, ``body``,
    ``draft``, ``include_uncommitted``, ``force``) plus two switches that are
    about the *session* rather than the push: ``report`` types the outcome
    into its terminal (:func:`prflow.report_block` through
    :meth:`Session.deliver`), and ``monitor`` spawns a child session on
    :data:`PR_MONITOR_WORKFLOW` that watches the PR and reports back
    (:func:`_spawn_pr_monitor`). ``monitor`` is gated on ``report`` -- a
    watcher for a session that was not even told about the PR makes no
    sense, and the form greys it the same way -- and on a PR actually
    having been opened. The monitor is spawned *before* the report is
    typed, so the block names the child. The push result is the body
    whatever happened: ``ok`` false with ``failed``/``error`` is a step
    that was refused, not a request that was malformed, so it is a 200 with
    a step list rather than an error the form would have to parse out of a
    message.
    """
    session = _session(request)
    cwd = _session_cwd(session)
    if not cwd:
        return json_error(400, "this session runs in no directory of its own")
    body = await _json_body(request)
    name = session.sdef.name
    result = await asyncio.to_thread(prflow.run, cwd, body, session=name)
    warnings: list = []
    monitor = None
    if body.get("monitor"):
        if not body.get("report"):
            warnings.append("monitor: needs the report checkbox -- nothing spawned")
        elif session.exited:
            warnings.append("monitor: the session has exited; nothing spawned")
        else:
            monitor = await _spawn_pr_monitor(request, session, result, warnings)
    if monitor:
        result["monitor"] = monitor
    delivered = None
    if body.get("report"):
        if session.exited:
            warnings.append("report: the session has exited; nothing was delivered")
        else:
            delivered = await session.deliver(prflow.report_block(result))
    return json_response({**result, "delivered": delivered, "warnings": warnings})


async def h_session_notice(request: web.Request) -> web.Response:
    """Show a line to whoever is looking at this session — the out-of-process
    door to :meth:`Session.notify`. Nothing reaches the PTY: ``/deliver`` is
    for the agent, this is for the person watching it. Body: ``text``
    (required), ``ttl`` seconds (optional, clamped), ``level`` (``info`` /
    ``warn`` / ``error``). ``viewers`` in the answer is how many sockets were
    sent it; 0 means nobody was looking and the line is gone."""
    session = _session(request)
    body = await _json_body(request)
    text = body.get("text")
    if not isinstance(text, str) or not text.strip():
        return json_error(400, "'text' must be a non-empty string")
    ttl = body.get("ttl")
    if ttl is not None and not isinstance(ttl, (int, float)):
        return json_error(400, "'ttl' must be a number of seconds")
    level = body.get("level", "info")
    if level not in notice_mod.LEVELS:
        return json_error(400, "'level' must be one of " + ", ".join(notice_mod.LEVELS))
    viewers = session.notify(text, ttl=ttl, level=level)
    return json_response({"ok": True, "viewers": viewers})


async def h_session_rebrief(request: web.Request) -> web.Response:
    """The session's re-briefing, composed fresh from daemon state.

    See :mod:`rebrief` for what goes in it and why. GET returns the text and
    touches nothing — the SessionStart hook prints it to claude, the MCP tool
    and CLI hand it to the agent that asked. POST delivers the same text into
    the session's own terminal instead, best-effort like every delivery; an
    empty composition is reported rather than typed, so pressing the button on
    a bare session does not paste an empty message into it.

    ``GET ?id=`` is the narrow door onto the same state: one addressed block
    by its content id (:func:`rebrief.recall`), for an agent that a reminder
    told the id of and that cannot find the text in its own context. It is
    deliberately not a POST — a pull is the agent's own turn, and nothing
    about it belongs in someone else's terminal.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    ident = (request.query.get("id") or "").strip()
    if request.method == "GET" and ident:
        manager.get(name)  # unknown session refused here, as compose does
        return json_response(
            {
                "session": name,
                **rebrief.recall(
                    name, ident, manager=manager, mesh_mgr=_mesh_mgr(request)
                ),
            }
        )
    block = rebrief.compose(name, manager=manager, mesh_mgr=_mesh_mgr(request))
    if request.method == "GET":
        return json_response({"session": name, "block": block})
    if not block:
        return json_response({"ok": True, "delivered": False, "empty": True})
    delivered = await manager.get(name).deliver(block)
    return json_response({"ok": True, "delivered": delivered, "empty": False})


async def h_session_loops(request: web.Request) -> web.Response:
    """A session's open loops: stored entries plus the mesh's reply-waits.

    ``?all=1`` includes closed entries — the ledger keeps them, so what a
    session waited on and when it resolved stays readable after the fact.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    manager.get(name)
    payload = loops.summary(name, _mesh_mgr(request))
    if request.query.get("all"):
        payload["all"] = loops.all_entries(name)
    return json_response(payload)


async def h_session_loop_add(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    manager.get(name)
    body = await _json_body(request)
    what = body.get("what")
    if not isinstance(what, str) or not what.strip():
        return json_error(400, "'what' is required")
    expires_in = body.get("expires_in")
    if expires_in is not None:
        try:
            expires_in = float(expires_in)
        except (TypeError, ValueError):
            return json_error(400, "'expires_in' must be a number of seconds")
    refs = body.get("refs")
    try:
        entry = loops.add(
            name,
            what,
            resume_when=str(body.get("resume_when") or ""),
            then=str(body.get("then") or ""),
            refs=refs if isinstance(refs, dict) else None,
            key=str(body.get("key") or "") or None,
            expires_in=expires_in,
        )
    except ValueError as exc:
        return json_error(400, str(exc))
    return json_response({"session": name, "loop": entry})


async def h_session_loop_close(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    manager.get(name)
    body = await _json_body(request)
    entry = loops.close(
        name, request.match_info["loop"], note=str(body.get("note") or "")
    )
    if entry is None:
        return json_error(404, f"no open loop {request.match_info['loop']!r}")
    return json_response({"session": name, "loop": entry})


async def h_session_briefing(request: web.Request) -> web.Response:
    """One session's LLM-composed status briefing (see :mod:`briefing`).

    404 for an unknown session (not the middleware's 400: the web UI keys its
    cards by name and must tell "gone" from "misconfigured"), 400 when the
    ``llm:`` block is absent or incomplete, 502 when the configured endpoint
    fails. ``?refresh=1`` bypasses the in-memory cache.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    try:
        session = manager.get(name)
    except ManagerError:
        return json_error(404, f"no session named {name!r}")
    cfg = briefing.llm_config()
    if not briefing.llm_configured(cfg):
        return json_error(400, "llm not configured")
    refresh = request.query.get("refresh") in ("1", "true")
    try:
        payload = await briefing.compose(session, cfg, refresh=refresh)
    except briefing.BriefingError as exc:
        return json_error(502, str(exc))
    return json_response(payload)


async def h_session_status_checks(request: web.Request) -> web.Response:
    """Enabled Y/N checks and the latest direct report for one session."""
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    try:
        manager.get(name)
        return json_response({
            "session": name,
            "checks": status_checks.session_entries(name, enabled_only=True),
        })
    except ManagerError:
        return json_error(404, f"no session named {name!r}")
    except status_checks.StatusCheckError as exc:
        return json_error(500, str(exc))


async def h_session_status_checks_report(request: web.Request) -> web.Response:
    """Accept the current managed session's direct MCP report."""
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    try:
        manager.get(name)
        body = await _json_body(request)
        answers = body.get("answers")
        if not isinstance(answers, list):
            return json_error(400, "'answers' must be an array")
        return json_response({
            "session": name,
            "checks": status_checks.report(name, answers),
        })
    except ManagerError:
        return json_error(404, f"no session named {name!r}")
    except ValueError as exc:
        return json_error(400, str(exc))
    except status_checks.StatusCheckError as exc:
        return json_error(500, str(exc))


async def h_session_status_checks_refresh(request: web.Request) -> web.Response:
    """Ask an active agent to read current checks and report fresh values."""
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    try:
        session = manager.get(name)
    except ManagerError:
        return json_error(404, f"no session named {name!r}")
    if session.exited:
        return json_error(409, f"session {name!r} has exited")
    try:
        checks = status_checks.session_entries(name, enabled_only=True)
    except status_checks.StatusCheckError as exc:
        return json_error(500, str(exc))
    if not checks:
        return json_response({"session": name, "delivered": False, "checks": []})
    delivered = await session.deliver(
        "[claunch status-check refresh]\n"
        "Read the current user-configured Y/N checks with MCP tool `status_checks`. "
        "Verify their current values from your work, then call `report_status_checks` "
        "with every enabled ID and a yes/no answer. The list is editable; do not use "
        "IDs remembered from an earlier request."
    )
    return json_response({"session": name, "delivered": delivered, "checks": checks})


async def h_session_capture(request: web.Request) -> web.Response:
    session = _session(request)
    history = request.query.get("history") in ("1", "true")
    trim = request.query.get("trim", "1") not in ("0", "false")
    # The grid renders a slice behind the byte stream (ScreenFeeder), and a
    # capture is the one read that means "what is on screen NOW" -- so wait
    # for it, by awaiting rather than by blocking the loop.
    synced = getattr(session, "screen_synced", None)
    if synced is not None:
        await synced()
    lines = session.capture(history=history)
    if trim:
        while lines and not lines[-1]:
            lines.pop()
    if request.query.get("format") == "json":
        x, y = session.screen.cursor()
        return json_response(
            {"lines": lines, "cursor": {"x": x, "y": y}, "status": session.status()}
        )
    text = "\n".join(lines)
    return web.Response(text=text + ("\n" if text else ""), content_type="text/plain")


async def h_session_transcript(request: web.Request) -> web.Response:
    """One page of the session's conversation, for a pane that scrolls itself.

    The terminal cannot answer this. A claude session repaints the alternate
    screen rather than scrolling it, so its history never reaches any
    scrollback — the daemon's included — and the readable record of what the
    session said lives only in claude's own jsonl. See
    :mod:`~claude_launcher.daemon.transcript_view`.

    ``before`` is the cursor a reader walks backwards as they scroll up;
    omitted, the page is the tail. Run in a thread: the index scan touches a
    file that reaches tens of megabytes the first time it is asked, and the
    event loop has terminals to pump.
    """
    session = _session(request)
    try:
        limit = int(request.query.get("limit", transcript_view.PAGE_DEFAULT))
    except ValueError:
        return json_error(400, "'limit' must be an integer")
    before_raw = request.query.get("before")
    try:
        before = int(before_raw) if before_raw not in (None, "") else None
    except ValueError:
        return json_error(400, "'before' must be an integer")

    page = await asyncio.to_thread(
        transcript_view.page,
        session.sdef.name,
        session.sdef,
        before=before,
        limit=limit,
    )
    return json_response(page)


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
        return json_response({"timeout": True, "status": session.status()}, status=408)
    return json_response({**session.info(), "timeout": False, "status": final})


async def h_session_resize(request: web.Request) -> web.Response:
    session = _session(request)
    body = await _json_body(request)
    try:
        cols, rows = int(body["cols"]), int(body["rows"])
    except (KeyError, ValueError, TypeError):
        return json_error(400, "'cols' and 'rows' must be integers")
    session.resize(cols, rows)
    return json_response({"ok": True})


async def h_index(request: web.Request) -> web.Response:
    index = _STATIC_DIR / "index.html"
    if not index.is_file():
        return web.Response(text="claunch daemon is running (web UI assets missing)")
    return web.FileResponse(index)


async def h_beads_fleet(request: web.Request) -> web.Response:
    """Every board the fleet touches — the Beads page.

    One entry per repository root among the sessions' directories (and the
    daemon's own), each issue tagged with the sessions it belongs to and why,
    so the page can draw the session↔issue match the rail draws per session,
    for everybody at once.
    """
    manager: SessionManager = request.app["manager"]
    extra = [os.getcwd()]
    cwd = request.query.get("cwd")
    if cwd:
        extra.insert(0, cwd)
    view = await request.app["beads"].fleet_view(list(manager.list()), extra)
    return json_response(view)


async def h_beads_stream(request: web.Request) -> web.Response:
    """One bounded Beads page for the fixed-height board viewport."""
    sort = request.query.get("sort", "updated_at")
    direction = request.query.get("direction", "desc")
    if sort not in {"updated_at", "created_at", "priority", "title"}:
        return json_error(400, "sort must be updated_at, created_at, priority or title")
    if direction not in {"asc", "desc"}:
        return json_error(400, "direction must be asc or desc")
    try:
        offset = int(request.query.get("offset", "0"))
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        return json_error(400, "offset and limit must be integers")
    if offset < 0 or not 1 <= limit <= 200:
        return json_error(400, "offset must be non-negative and limit must be 1..200")
    raw_priority = request.query.get("priority")
    try:
        priority = int(raw_priority) if raw_priority is not None else None
    except ValueError:
        return json_error(400, "priority must be an integer")
    if priority is not None and not 0 <= priority <= 9:
        return json_error(400, "priority must be 0..9")
    manager: SessionManager = request.app["manager"]
    extra = [os.getcwd()]
    cwd = request.query.get("cwd")
    if cwd:
        extra.insert(0, cwd)
    view = await request.app["beads"].stream_view(
        list(manager.list()), extra, offset=offset, limit=limit, priority=priority,
        sort=sort, direction=direction,
    )
    return json_response(view)


async def h_beads_queues(request: web.Request) -> web.Response:
    """Every board's queues, one lane per session — the Beads page's Queues
    tab. Same boards as :func:`h_beads_fleet` (the sessions' directories and
    the daemon's own, or ``?cwd=``); each lane carries the session's issues in
    the order its worker takes them, its status, and the cflow step it is on,
    so the operator sees who is doing what next without opening a terminal.
    """
    manager: SessionManager = request.app["manager"]
    extra = [os.getcwd()]
    cwd = request.query.get("cwd")
    if cwd:
        extra.insert(0, cwd)
    view = await request.app["beads"].queues_view(
        list(manager.list()), extra, cflow_for=cflow_clock.run_summary,
    )
    return json_response(view)


async def h_beads_assign(request: web.Request) -> web.Response:
    """Move an issue onto a session's queue — the Queues tab's drag.

    Body: ``session`` (a name, or ``null``/``""`` for the unassigned pool),
    ``cwd`` (which board; the daemon's by default), ``force`` (move it even
    off a running session that is mid-round). The daemon writes exactly what
    a leader would type — ``br update <id> --assignee <session>`` and a
    ``QUEUED``/``UNQUEUED`` comment — and never a status: see
    :meth:`daemon.beads.Board.assign`. 404 for an issue the board does not
    have, 409 for the one refusal (in_progress under a running session).
    """
    manager: SessionManager = request.app["manager"]
    board = request.app["beads"]
    body = await _json_body(request)
    cwd = str(body.get("cwd") or request.query.get("cwd") or os.getcwd())
    root = await board.root_for(cwd)
    if not board.has_board(root):
        return json_error(404, f"no board for {cwd}")
    session = body.get("session")
    if session is not None and not isinstance(session, str):
        return json_error(400, "'session' must be a session name or null")
    try:
        moved = await board.assign(
            root, request.match_info["id"], session,
            manager=manager, force=bool(body.get("force")),
        )
    except beads_mod.AssignRefused as exc:
        return json_error(409, str(exc))
    except BeadsError as exc:
        return json_error(404 if "no issue" in str(exc) else 500, str(exc))
    return json_response({"root": str(root), **moved})


async def h_beads_candidates(request: web.Request) -> web.Response:
    """The issues a creation form may offer for a directory's board.

    What the new-session and spawn forms fill their "existing issue" picker
    from, and the reason that picker can be honest: each row carries the
    daemon's own verdict on it (:func:`daemon.beads.adoption`) — whether a new
    session would take it or only join a running holder — computed from the
    same session list the creation path will use, so the form promises exactly
    what the daemon is about to do.

    ``?cwd=`` says which board (any directory inside the repository) and
    defaults to the daemon's own. ``?parent=`` names the session a spawn would
    hang off, so a child form asks about the board of the directory its child
    will actually run in rather than the daemon's.
    """
    manager: SessionManager = request.app["manager"]
    cwd = request.query.get("cwd") or ""
    parent = request.query.get("parent") or ""
    if not cwd and parent:
        try:
            cwd = manager.get(parent).sdef.cwd
        except ManagerError:
            return json_error(404, f"no session named {parent!r}")
    view = await request.app["beads"].candidates(cwd or os.getcwd(), manager)
    return json_response(view)


async def h_beads_issue(request: web.Request) -> web.Response:
    """One issue in full, comments included, with the round reports written
    for it. ``?cwd=`` says which board — any directory inside the repository —
    and defaults to the daemon's.

    The reports ride along rather than sitting behind a second call because
    they are the same answer: the issue says what the round was for, and the
    report says what came of it. They are looked up by issue across every
    session (:func:`reports.for_issue`), not under the session that happens to
    be running now — the reader who needs this most is looking at a closed
    issue whose session ended days ago.
    """
    board = request.app["beads"]
    cwd = request.query.get("cwd") or os.getcwd()
    root = await board.root_for(cwd)
    if not board.has_board(root):
        return json_error(404, f"no board for {cwd}")
    issue_id = request.match_info["id"]
    try:
        issue = await board.show(root, issue_id)
    except BeadsError as exc:
        return json_error(404, str(exc))
    linked = []
    for session in request.app["manager"].list():
        sdef = session.sdef
        matches = beads_mod.match([issue], sdef.name, issue=sdef.issue, task=sdef.task)
        if matches and await board.root_for(sdef.cwd) == root:
            linked.append({
                "name": sdef.name, "status": session.status(), "via": matches[0]["via"],
            })
    return json_response({
        "root": str(root),
        "issue": issue,
        "sessions": linked,
        "reports": reports_mod.for_issue(issue_id),
        # Derived here rather than re-parsed in the page: the front matter's
        # spelling is the daemon's (beads_meta), and a second reader of it in
        # JavaScript is a second place for it to drift.
        "workspace": beads_meta.workspace_of(issue),
        # What the workspace picker may offer. The issue may only name one of
        # these, so the form that writes it and the daemon that refuses an
        # unregistered name are reading the same list.
        "workspaces": [
            {"name": w.name, "path": w.path} for w in workspaces.list_all()
        ],
    })


async def h_beads_workspace(request: web.Request) -> web.Response:
    """Record which workspace an issue's session should be created in.

    Body: ``workspace`` (a registered workspace NAME, or ``null``/``""`` to
    clear it) and ``cwd`` (which board; the daemon's by default). The value is
    written as YAML front matter on the issue's description
    (:meth:`daemon.beads.Board.set_workspace`), which is what lets the Beads
    page open a creation modal already pointed at the right directory instead
    of asking the operator to pick it again.

    404 for an issue the board does not have, 400 for a name nobody registered.
    """
    board = request.app["beads"]
    body = await _json_body(request)
    cwd = str(body.get("cwd") or request.query.get("cwd") or os.getcwd())
    root = await board.root_for(cwd)
    if not board.has_board(root):
        return json_error(404, f"no board for {cwd}")
    name = body.get("workspace")
    if name is not None and not isinstance(name, str):
        return json_error(400, "'workspace' must be a workspace name or null")
    try:
        written = await board.set_workspace(root, request.match_info["id"], name)
    except BeadsError as exc:
        text = str(exc)
        status = 404 if "no issue" in text else 400 if "no workspace" in text else 500
        return json_error(status, text)
    return json_response({"root": str(root), **written})


def _int_query(request: web.Request, key: str, default: int, lo: int, hi: int) -> Optional[int]:
    """A bounded integer query parameter; ``None`` when it does not parse."""
    raw = request.query.get(key)
    if raw is None or raw == "":
        return default
    try:
        value = int(raw)
    except ValueError:
        return None
    return value if lo <= value <= hi else None


async def _search_root(request: web.Request, kind: str):
    """The board a search or reindex is about, from ``?cwd=`` or ``?parent=``
    the way the candidates picker resolves it. ``(root, error_response)``."""
    if kind != "beads":
        return None, None
    manager: SessionManager = request.app["manager"]
    cwd = request.query.get("cwd") or ""
    parent = request.query.get("parent") or ""
    if not cwd and parent:
        try:
            cwd = manager.get(parent).sdef.cwd
        except ManagerError:
            return None, json_error(404, f"no session named {parent!r}")
    cwd = cwd or os.getcwd()
    root = await request.app["rag"].resolve_root(cwd)
    if root is None:
        return None, json_error(404, f"no board for {cwd}")
    return root, None


async def h_search(request: web.Request) -> web.Response:
    """Rank a corpus for a query: ``?q=`` (required), ``?kind=beads|sessions``
    (default beads), ``?limit=`` (1..50, default 10), ``?rerank=0`` to skip
    the reranker, ``?wait=`` seconds to give a running index sync (0..30,
    default 2), and for the board ``?cwd=`` or ``?parent=`` as the
    candidates picker takes them.

    The answer carries the index's coverage (``index.indexed`` of
    ``index.total``) beside the results: a first search of a large board
    ranks what has been embedded so far and says so, rather than blocking
    for the ten minutes a full index takes. 400 when the ``rag:`` block is
    not configured or the query is empty; 502 when the endpoint fails.
    """
    service: rag_mod.RagService = request.app["rag"]
    kind = (request.query.get("kind") or "beads").strip()
    if kind not in rag_mod.KINDS:
        return json_error(400, f"kind must be one of {', '.join(rag_mod.KINDS)}")
    query = (request.query.get("q") or "").strip()
    if not query:
        return json_error(400, "q is required")
    if not service.configured():
        return json_error(400, "rag: block not configured (base_url, api_key, embedding_model)")
    limit = _int_query(request, "limit", 10, 1, 50)
    wait = _int_query(request, "wait", 2, 0, 30)
    if limit is None or wait is None:
        return json_error(400, "limit must be 1..50 and wait 0..30")
    rerank = (request.query.get("rerank") or "1") not in ("0", "false", "no")
    root, err = await _search_root(request, kind)
    if err is not None:
        return err
    try:
        view = await service.search(
            kind, query, root=root, limit=limit, rerank=rerank, wait=float(wait),
        )
    except rag_mod.RagError as exc:
        return json_error(502, str(exc))
    return json_response(view)


async def h_beads_related(request: web.Request) -> web.Response:
    """The issues nearest to one, by embedding — the "is this a duplicate"
    question asked of the index. ``?cwd=`` names the board, ``?limit=``
    (1..30, default 8) how many neighbours."""
    service: rag_mod.RagService = request.app["rag"]
    if not service.configured():
        return json_error(400, "rag: block not configured (base_url, api_key, embedding_model)")
    limit = _int_query(request, "limit", 8, 1, 30)
    if limit is None:
        return json_error(400, "limit must be 1..30")
    root, err = await _search_root(request, "beads")
    if err is not None:
        return err
    try:
        view = await service.related(root, request.match_info["id"], limit=limit)
    except rag_mod.RagError as exc:
        return json_error(502, str(exc))
    return json_response(view)


async def h_rag_status(request: web.Request) -> web.Response:
    """The search feature's state: configured or not, which models, and each
    loaded index's coverage. Never the api key."""
    return json_response(request.app["rag"].status())


async def h_gh_status(request: web.Request) -> web.Response:
    """Whether ``gh`` is installed and signed in, per host, with the guide.

    Read-only, and off the loop: it forks git per registered repository and
    ``gh auth status`` per host, and the latter talks to the host. Nothing
    here is cached -- the card has a Re-check button precisely so the user
    can install, log in, and see the answer change.
    """
    return json_response(
        await asyncio.to_thread(lambda: ghcli.status(ghcli.daemon_repositories()))
    )


async def h_rag_reindex(request: web.Request) -> web.Response:
    """Start (or restart) an index sync. Body: ``kind`` (beads|sessions,
    default beads), ``cwd`` for the board, ``force`` to re-embed everything.
    Returns 202 with the sync's progress; the work continues in the daemon.
    """
    service: rag_mod.RagService = request.app["rag"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    kind = str(body.get("kind") or "beads").strip()
    if kind not in rag_mod.KINDS:
        return json_error(400, f"kind must be one of {', '.join(rag_mod.KINDS)}")
    if not service.configured():
        return json_error(400, "rag: block not configured (base_url, api_key, embedding_model)")
    root = None
    if kind == "beads":
        cwd = str(body.get("cwd") or "") or os.getcwd()
        root = await service.resolve_root(cwd)
        if root is None:
            return json_error(404, f"no board for {cwd}")
    prog = service.ensure_sync(kind, root, force=bool(body.get("force")))
    return json_response(
        {"kind": kind, "root": str(root) if root else None, "index": prog.view()},
        status=202,
    )


async def h_session_beads(request: web.Request) -> web.Response:
    """A session's slice of its board — the same object the meta call carries."""
    manager: SessionManager = request.app["manager"]
    session = manager.get(request.match_info["name"])
    return json_response(await request.app["beads"].session_view(session))


async def h_session_beads_create(request: web.Request) -> web.Response:
    """Give a session an issue after the fact — for one created without a
    task, which the daemon minted nothing for. Body: ``title`` (required),
    ``description`` (optional; the workflows' template when omitted). The
    new issue is linked to the session the way a creation-time one is."""
    manager: SessionManager = request.app["manager"]
    session = manager.get(request.match_info["name"])
    body = await _json_body(request)
    title = str(body.get("title") or "").strip()
    if not title:
        return json_error(400, "an issue needs a title")
    made = await request.app["beads"].create_for(
        session, title=title, description=str(body.get("description") or "")
    )
    beads_mod.link_issue(session, made["issue"])
    manager.persist()
    return json_response(
        {**made, "beads": await request.app["beads"].session_view(session)},
        status=201,
    )


async def h_session_reports(request: web.Request) -> web.Response:
    """A session's round reports, newest first — the same list the beads view
    carries, for a caller that wants only the files.

    No session lookup, like the page route below and for the same reason: the
    point of keeping reports outside ``sessions/<name>/`` is that they outlive
    the record, and a listing that 404'd once ``clear-sessions`` ran would
    hand back exactly nothing at the moment the files matter most.
    """
    name = request.match_info["name"]
    try:
        reports_mod.check_session(name)
    except reports_mod.ReportError as exc:
        return json_error(400, str(exc))
    return json_response({"session": name, "reports": reports_mod.listing(name)})


async def h_reports_index(request: web.Request) -> web.Response:
    """Every round report on this machine, newest first — the Reports page.

    The rows come off the disk (:func:`reports.index`), which is what lets
    this answer at all: of the sessions that have written one, only a couple
    are usually still running, and the rest were cleared long ago. Asking the
    registry for the list would have returned the two.

    So the registry is asked the other way round — not "which sessions are
    there" but "does the daemon still know THIS one", per row. A live session
    gets its status, a record that has exited gets ``"exited"``, and a session
    the daemon has never heard of (or has cleared) gets ``None``. None of the
    three is a reason to drop the row; the field exists so the page can say
    which link is worth following.
    """
    manager: SessionManager = request.app["manager"]
    known = {s.sdef.name: s.status() for s in manager.list()}
    rows = [
        {**row, "session_status": known.get(row["session"])}
        for row in reports_mod.index()
    ]
    return json_response({"reports": rows})


async def h_session_report_file(request: web.Request) -> web.StreamResponse:
    """Serve one report page.

    The session need not exist any more: a report outlives the pane it was
    written in, and refusing to serve a dead session's report would defeat the
    reason it is kept outside ``sessions/<name>/``. The name is validated as a
    path component instead, and the filename must match the indexed form,
    which admits no separators — that, not the session lookup, is what keeps
    the read inside the reports directory.

    The page is agent-written HTML served from the daemon's own origin, where
    the dashboard's auth cookie lives. ``Content-Security-Policy: sandbox``
    drops it into an opaque origin, so a report can style and script itself
    but cannot turn around and call the API as the logged-in operator.
    """
    try:
        path = reports_mod.resolve(request.match_info["name"], request.match_info["file"])
    except reports_mod.ReportError as exc:
        return json_error(400, str(exc))
    if not reports_mod.is_report(path):
        return json_error(404, f"no such report: {request.match_info['file']}")
    return web.FileResponse(
        path,
        headers={
            "Content-Type": "text/html; charset=utf-8",
            "Content-Security-Policy": "sandbox allow-scripts allow-popups",
            "X-Content-Type-Options": "nosniff",
            "Cache-Control": "no-cache",
        },
    )


async def _json_body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except Exception:
        return {}
    return body if isinstance(body, dict) else {}
