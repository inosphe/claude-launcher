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
import hashlib
import json
import logging
import os
import re
import secrets
import socket
import time
from datetime import datetime, timezone
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Dict, List, Optional, Set, Tuple
from urllib.parse import urlsplit

from aiohttp import WSMsgType, web

from .. import __version__, borrowing, harness_policy, harnesses as harness_registry
from .. import (
    pi_provider,
    credentials,
    lineage,
    metering,
    profile as profile_mod,
    provider_spec,
    providers,
    quickjob,
    reports as reports_mod,
    runner,
    translators,
    usage,
    profile_usage,
)
from .. import auto_compact as auto_compact_mod
from .. import install
from .. import install_plan
from .. import session_commits
from .. import beads_db, beads_meta
from .. import ghcli, prflow, projects, reposync, reposync_flow, spawn as spawn_mod, store, workspaces
from .. import workspace_usage
from .. import plugins, prompt_packs, settings
from .. import worktree as worktree_mod
from .. import wsl_reg_stub
from . import actors, beads as beads_mod, channel, clipboard, connections
from . import p2p
from . import opening_images
from . import handoff as handoff_mod
from . import loop_lag as loop_lag_mod
from . import line_sync
from . import notice as notice_mod
from . import rag as rag_mod
from . import shared_reads
from . import worktree_inventory
from . import work_checkout
from . import vscode as vscode_mod
from . import cflow2_approve, cflow2_web
from . import session_timers, session_prs, session_procs
from ..cflow2 import expr as cflow2_expr, frontend as cflow2_frontend
from ..cflow2 import ir as cflow2_ir, runtime as cflow2_runtime, store as cflow2_store
from ..cflow2 import layers as cflow2_layers
from . import (
    briefing, cflow_clock, clipty, ctxsize, loops, onboard, prompt_presets, prompter,
    rebrief, session_input, status_checks,
)
from . import paths
from . import daemon_prompt
from . import sessionstats, statscompare, statsworker
from . import tokenusage
from . import exited_rows
from . import transcript_view
from . import window as window_mod
from .. import cli_beads
from ..cli_beads import BeadsError
from ..cflow import editor as cflow_editor, engine as cflow_engine, state as cflow_state
from ..cflow.engine import CflowError
from ..cflow.model import WorkflowError
from ..cflow.state import LockBusy, StateError
from ..profile import ProfileError
from . import goto_gate
from . import mesh_roles
from . import reposync_gate
from . import reposync_agent
from . import host_code, restart_gate
from . import restart_notice
from .harness import PI_HARNESS, HarnessError, SessionDef, check_kind
from . import score_goal
from .manager import MAX_NOTE, ManagerError, SessionManager
from . import mesh as mesh_mod
from .mesh import MeshBusy, MeshConflict, MeshError, MeshManager
from .session import STATUS_IDLE, KeyboardHeld, SessionGone
from . import session as session_mod
from . import ws as ws_mod
from . import shadow as shadow_mod

#: Prefix of the login cookie. The full name carries a digest of this
#: daemon's token (``cookie_name``): browsers scope cookies by host, not by
#: port, so two daemons on one machine (``:8378`` and a named instance on
#: ``:8379``) under one fixed name overwrote each other's login on every
#: request, and the page fell back to the paste-the-token prompt
#: (claunch-id6sp).
COOKIE_PREFIX = "claunch_session"


def cookie_name(token: str) -> str:
    """The login cookie's name for the daemon that holds ``token``.

    Stable across restarts of one daemon (the token file outlives them) and
    different between daemons, whose tokens are minted independently. A
    rotated token renames the cookie, which costs nothing: the old cookie's
    session died with the rotation anyway.
    """
    digest = hashlib.sha256(token.encode("utf-8")).hexdigest()[:12]
    return f"{COOKIE_PREFIX}_{digest}"

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
    return json_text_response(_dumps(payload), status=status, headers=headers)


def json_text_response(text: str, *, status: int = 200, headers=None) -> web.Response:
    """:func:`json_response` for a body that is already JSON text."""
    resp = web.Response(
        text=text, status=status, headers=headers, content_type="application/json",
    )
    if len(text) >= JSON_GZIP_MIN:
        resp.enable_compression()
    return resp


class JSONText(str):
    """A route's JSON body, carried as the text the route wrote.

    A batched read used to parse each route's body back into objects, only
    for the batch to encode them again around it: two passes over every
    payload on the loop that pumps the terminals, for bytes that were
    already right (claunch-xrrn0 -- JSON was 14.8% of the daemon's leaves,
    and the control socket's reads alone 11.1% of the loop). A value of this
    type is spliced into the reply as written; see :func:`_with_answers`.
    """


def _with_answers(head: dict, answers: dict) -> str:
    """``head`` as JSON text with an ``answers`` member spliced in last.

    The reply of a batch and of a control-socket read: everything but the
    answers is small and encoded here; a :class:`JSONText` answer is the
    route's own text, anything else (a read with no body, a test's stub) is
    encoded. Joined once, so each answer is copied once: a rail is a
    megabyte, and every intermediate string would be another.
    """
    text = _dumps(head)
    parts = [text[:-1], ", " if head else "", '"answers": {']
    for i, (key, value) in enumerate(answers.items()):
        if i:
            parts.append(", ")
        parts += [_dumps(key), ": ",
                  value if isinstance(value, JSONText) else _dumps(value)]
    parts.append("}}")
    return "".join(parts)


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
    """Make the dashboard's own assets revalidate, and release the
    connection each one was served on.

    Two things, because the page's assets cause two separate problems and
    both are settled in the same place -- the response that carries them.

    *Revalidation.* ``index.html`` names ``static/app.js`` with no version,
    and aiohttp's static handler sends no ``Cache-Control``, so browsers
    fall back to *heuristic* freshness (a fraction of the file's age) and
    serve a stale bundle without ever asking us. A daemon that has already
    been upgraded then keeps rendering the old UI, which reads as "the fix
    did not ship". ``no-cache`` does not mean "do not store": the ETag and
    Last-Modified the static handler already sends turn each load into a
    conditional GET that answers 304 in a couple of hundred bytes.

    *Release.* A browser keeps a connection in its pool after the answer
    arrives -- Firefox for 115 seconds by default -- and counts it against
    a per-server ceiling of six. This page asks for twelve assets on a
    load, so the load alone fills that pool, and for the next two minutes a
    terminal's WebSocket upgrade waits in the browser's own connection
    queue without ever being sent. Measured: at 10:02:56 six firefox
    connections opened for ``app.js``, ``observer.js``, ``search-anything
    .js``, ``diagram-viewport.js``, ``session-tabs.js`` and one ``/api/
    batch``; from 10:03:02 to 10:03:34 the page reported twenty terminal
    sockets it could not open (claunch-6j09). Closing the connection with
    the response returns the slot when the asset is delivered rather than
    two minutes later. It costs one TCP handshake per asset, which over
    127.0.0.1 is not a cost worth keeping the pool for.

    Both apply to the page and its assets only. ``/api/`` is left alone: it
    is where the reads live, and they are on a socket of their own.
    """
    response = await handler(request)
    path = request.path
    if path == "/" or path.startswith("/static/"):
        response.headers.setdefault("Cache-Control", "no-cache")
        response.force_close()
    return response


#: A refused cflow v2 operation or an unreadable v2 definition/run: a 400
#: with the message, like the v1 engine's errors.
_CFLOW2_ERRORS = (
    cflow2_runtime.FlowError, cflow2_store.StoreError, cflow2_frontend.FrontendError,
    cflow2_ir.IRError, cflow2_expr.ExprError,
)


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
    except (SessionGone, MeshConflict, LockBusy, KeyboardHeld, restart_gate.GateBusy,
            goto_gate.GateBusy) as exc:
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
        cflow2_web.ChoiceError,
    ) as exc:
        if isinstance(exc, cflow2_web.InputsInvalid):
            # per-field errors for the start form (docs/cflow-v2.md 2.6)
            return json_response({"error": str(exc), "errors": exc.errors}, status=400)
        return json_error(400, str(exc))
    except _CFLOW2_ERRORS as exc:
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
    cookie_key = cookie_name(token)

    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        path = request.path
        # /vscode/ is a proxy into code-server, which runs with no auth of its
        # own (daemon/vscode.py): this gate is the only one in front of it.
        gated = path.startswith("/api/") or path.startswith("/vscode/")
        if not gated or path == "/api/health" or path == "/api/auth/session":
            return await handler(request)
        auth = request.headers.get("Authorization", "")
        if auth.startswith("Bearer ") and _token_eq(auth[7:], token):
            request[actors.AUTH_VIA_KEY] = actors.AUTH_BEARER
            return await handler(request)
        cookie = request.cookies.get(cookie_key)
        if cookie and cookie in cookie_sessions:
            # How it authenticated is the one thing that tells the dashboard
            # from a shell: actors.from_request reads it (claunch-o6xws).
            request[actors.AUTH_VIA_KEY] = actors.AUTH_COOKIE
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
        if path.startswith("/vscode/"):
            # An editor tab is a page, not a fetch: say where to log in.
            return web.Response(
                status=401, content_type="text/html",
                text="<!doctype html><meta charset=utf-8><title>VS Code</title>"
                "<p style='font:15px system-ui;margin:2em'>Log in to the claunch "
                "dashboard first, then open VS Code from it again.</p>",
            )
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
    shared_read_max_age: float = 0.0,
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
            # Inside auth, so only a write that was let in drops a shared
            # answer (claunch-jkvpv).
            shared_reads.build_middleware(),
        ]
    )
    app["manager"] = manager
    # One answer per read for the routes every open dashboard polls; off
    # (0) unless the daemon's configuration turns it on -- see
    # daemon/shared_reads.py.
    app["shared_reads"] = shared_reads.SharedReads(shared_read_max_age)
    app["mesh"] = mesh if mesh is not None else MeshManager(manager)
    from . import mesh_chat, observer, operator_bot
    observer.install(app)
    mesh_chat.install(app)
    operator_bot.install(app, create=_create_session, gates=_operator_gates,
                         work=lambda sessions: _operator_work(app, sessions))
    from . import local_sync_api
    local_sync_api.install(app)
    clipboard.install(app)
    vscode_mod.install(app)
    app["relay_state"] = relay_state if relay_state is not None else _relay_unconfigured
    app["token"] = token
    app["cookie_sessions"] = cookie_sessions
    app["cookie_name"] = cookie_name(token)
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
    #: How the shutdown now in progress treats the PTY host and its sessions
    #: (claunch-xnnct.1): "keep" leaves them running for the successor to
    #: adopt, "full" and "stop" end them and the host. Read by ``__main__``
    #: at teardown. "stop" until a request says otherwise -- a shutdown
    #: nobody named (a signal) ends everything, as without a host.
    app["restart_mode"] = "stop"
    #: The approval gate the agent path waits behind; see restart_gate.
    app["restart_gate"] = restart_gate.RestartGate(app, timeout=gate_timeout)
    #: The approval gate a leader's child-run goto waits behind; see
    #: goto_gate. Its nudge is the cflow action handlers' own helper, so the
    #: settled move reaches the child driver the same way a human's goto does.
    #: The approval gate a session's repository sync waits behind; see
    #: reposync_gate. Unanswered expires as a refusal.
    app["reposync_gate"] = reposync_gate.ReposyncGate()
    # workspace name -> the disk-usage measurement running for it (claunch-2oait)
    app["ws_usage_tasks"] = {}
    app["ws_sync_locks"] = {}
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
    # The remote-shadow directory (daemon/shadow.py), built on first read:
    # a holder rather than a key set later, because the app is frozen by then.
    app["shadow"] = {}
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
    # Mesh audience selectors (``to: "@in_review"``) resolve against this
    # board; the mesh manager itself knows nothing about boards.
    app["mesh"].audience_resolver = functools.partial(
        _mesh_audience, manager, board
    )
    manager.exit_hooks.append(board.session_exited)
    # A failed session's sweep waits for the person settling it
    # (claunch-0j6gb.1); test registries may not have the list.
    settle_hooks = getattr(manager, "settle_hooks", None)
    if settle_hooks is not None:
        settle_hooks.append(board.session_exited)
    # A finished sweep stamps ``swept_at`` on the session; persisting right
    # after is what keeps the next restart from sweeping that ending again.
    # Tests hand in registries without persist.
    persist = getattr(manager, "persist", None)
    if persist is not None:
        board.swept_hooks.append(persist)
    # The reserved default board. Pinned once, to the board this daemon was
    # already using, so every issue filed before workspaces had boards of
    # their own keeps reading as that one's -- nothing is copied and nothing
    # moves. Afterwards it is an ordinary setting on the Settings page like
    # any other board's.
    #
    # Two candidates, because the daemon's working directory alone is not
    # enough: a daemon launched from the home directory has no checkout
    # there, and pinned nothing at all. The checkout this code runs from is
    # the second answer -- in an editable install it is the repository whose
    # board every session was filing into. It goes through repo_root like the
    # first, so a daemon running out of a linked worktree names the main
    # checkout's board (a worktree holds no database of its own). Only a root
    # that already holds a database is pinned.
    try:
        source = beads_db.source_checkout()
        beads_db.ensure_default(beads_db.pick_default_root([
            cli_beads.repo_root(os.getcwd()),
            cli_beads.repo_root(str(source)) if source is not None else None,
        ]))
    except Exception:  # an unwritable config must not stop the daemon
        log.exception("beads: could not pin the %s board", beads_db.DEFAULT_BOARD)
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
    from . import search_anything
    search_anything.install(app)
    from . import glossary_api
    glossary_api.install(app)
    from . import spec_api
    spec_api.install(app)
    # World-model simulations (daemon/sim_api.py, claunch-efnbt): records,
    # ownership (released by an exit hook it adds), rounds, owner notices.
    from . import sim_api
    sim_api.install(app)
    # Session QA (daemon/qa_api.py, claunch-l8vyi): questions a session puts
    # to the user, answered in its QA pane and typed back into it.
    from . import qa_api
    qa_api.install(app)
    # Turn-end QA nudges (daemon/qa_nudge.py, claunch-165w6): a session that
    # ended its turn asking the user in plain text is told to use qa_ask.
    # After qa_api: the clock reads app["qa"] for the agent's own writes.
    from . import qa_nudge
    qa_nudge.install(app)
    # Session threads (daemon/threads_api.py, claunch-doj98): sub-topic
    # conversations beside the terminal, typed into the session by id.
    from . import threads_api
    threads_api.install(app)
    # deep_think (daemon/deep_think.py): a session asks itself a question and
    # a quick-fork of it answers. Needs app["handoff"] (set above): the answer
    # is a merge, and the module hooks how that merge is written and kept.
    from . import deep_think
    deep_think.install(app)
    app.on_startup.append(_start_rag)
    app.on_shutdown.append(_stop_rag)
    # How late the loop runs its callbacks: the part of the page's round
    # trip that is the daemon being busy (claunch-y9ax9).
    try:
        lag_threshold = float(
            store.daemon_config().get("loop_lag_log_s", loop_lag_mod.LOG_THRESHOLD)
        )
    except (TypeError, ValueError):
        lag_threshold = loop_lag_mod.LOG_THRESHOLD
    app["loop_lag"] = loop_lag_mod.LoopLag(log_threshold=lag_threshold)
    app.on_startup.append(_start_loop_lag)
    app.on_shutdown.append(_stop_loop_lag)
    # The machine's CPU, memory and disk I/O for the rail header and the
    # System performance page, with a rotating sample log (claunch-zuk0b).
    from . import sysperf as sysperf_mod
    try:
        sysperf_interval = float(
            store.daemon_config().get("sysperf_interval_s", sysperf_mod.INTERVAL)
        )
    except (TypeError, ValueError):
        sysperf_interval = sysperf_mod.INTERVAL
    sysperf_mod.install(app, interval=sysperf_interval)
    # The Stats page reads transcripts in a child process, off this loop's
    # GIL (claunch-3r94d); started on first use.
    app["stats_worker"] = statsworker.StatsWorker()
    app.on_shutdown.append(_stop_stats_worker)
    # The measurement window: one per daemon, injected for tests. Its
    # session_exited rides the same exit funnel as the board's, for the same
    # reason: a holder that dies must release without a human noticing.
    window = window if window is not None else window_mod.WindowManager(manager)
    app["window"] = window
    manager.exit_hooks.append(window.session_exited)
    from . import artifact_cleanup_api
    artifact_cleanup_api.install(app)
    # A restart never lets some endings reach the hook above: restore_all
    # retires what it does not relaunch before this board exists, so each
    # retired record's in_progress issues would keep claiming a dead session
    # is working. Sweep the board for them here, where board and loop exist.
    board.sessions_exited(manager.take_retired_for_sweep())

    r = app.router
    r.add_get("/api/health", h_health)
    # Open sockets as state (daemon/connections.py), for `claunch connections`
    # and for anyone diagnosing a terminal that will not come up.
    r.add_post("/api/batch", h_batch)
    r.add_get("/api/control/ws", h_control_ws)
    r.add_get("/api/connections", h_connections)
    r.add_get("/api/shared-reads", h_shared_reads)
    r.add_post("/api/connections/close", h_connections_close)
    # The measurement window (daemon/window.py): the machine's test-run
    # arbiter as readable state — who holds it, who waits — so the sweep
    # protocol stops standing on process scans and mesh chat.
    r.add_get("/api/window", h_window_status)
    r.add_post("/api/window/acquire", h_window_acquire)
    r.add_post("/api/window/release", h_window_release)
    r.add_post("/api/window/cancel", h_window_cancel)
    r.add_post("/api/window/prioritize", h_window_prioritize)
    r.add_post("/api/window/force", h_window_force)
    r.add_post("/api/window/report", h_window_report)
    r.add_get("/api/window/history", h_window_history)
    r.add_get("/api/window/limits", h_window_limits)
    r.add_put("/api/window/limits", h_window_set_limits)
    r.add_post("/api/auth/session", h_auth_session)
    r.add_get("/api/daemon", h_daemon_info)
    r.add_post("/api/daemon/shutdown", h_daemon_shutdown)
    r.add_post("/api/daemon/restart", h_daemon_restart)
    r.add_get("/api/daemon/restart-check", h_daemon_restart_check)
    # The approval gate for agent-requested restarts (see
    # :mod:`restart_gate`): one request, its state, and the two settlements.
    r.add_get("/api/daemon/restart-request", h_restart_request_get)
    r.add_post("/api/daemon/restart-request", h_restart_request_submit)
    r.add_post("/api/daemon/restart-request/approve", h_restart_request_approve)
    r.add_post("/api/daemon/restart-request/reject", h_restart_request_reject)
    r.add_post("/api/daemon/restart-request/extend", h_restart_request_extend)
    r.add_get("/api/profiles", h_profiles)
    r.add_post("/api/profiles/permission-mode", h_profiles_permission_mode)
    r.add_post("/api/profiles/pi-tui-mode", h_profiles_pi_tui_mode)
    r.add_get("/api/install", h_install_overview)
    r.add_get("/api/install/plan", h_install_plan)
    r.add_post("/api/install/apply", h_install_apply)
    r.add_get("/api/usage", h_usage)
    r.add_get("/api/metering", h_metering)
    r.add_get("/api/metering/live", h_metering_live)
    r.add_get("/api/borrow-options", h_borrow_options)
    r.add_get("/api/roles", h_roles)
    r.add_get("/api/projects", h_projects)
    r.add_post("/api/projects", h_project_add)
    r.add_patch("/api/projects/{name}", h_project_update)
    r.add_delete("/api/projects/{name}", h_project_remove)
    r.add_get("/api/workspaces", h_workspaces)
    r.add_get("/api/workspaces/usage", h_workspace_usage)
    r.add_post("/api/workspaces/{name}/usage", h_workspace_usage_measure)
    r.add_get("/api/git", h_git)
    r.add_get("/api/worktrees", h_worktrees)
    r.add_get("/api/worktrees/changes", h_worktree_changes)
    r.add_post("/api/worktrees/remove", h_worktrees_remove)
    r.add_post("/api/workspaces", h_workspace_add)
    r.add_delete("/api/workspaces/{name}", h_workspace_remove)
    r.add_get("/api/prompt-packs", h_prompt_packs)
    r.add_get("/api/prompt-packs/offer", h_prompt_packs_offer)
    r.add_get("/api/prompt-packs/text", h_prompt_packs_text)
    r.add_get("/api/daemon-prompt", h_daemon_prompt_get)
    r.add_put("/api/daemon-prompt", h_daemon_prompt_put)
    r.add_post("/api/prompt-packs/install", h_prompt_packs_install)
    r.add_post("/api/prompt-packs/install-all", h_prompt_packs_install_all)
    r.add_post("/api/prompt-packs/remove", h_prompt_packs_remove)
    r.add_put("/api/prompt-packs/role-packs", h_prompt_packs_role_packs)
    r.add_get("/api/briefing/llm", h_briefing_llm)
    r.add_put("/api/briefing/llm", h_briefing_llm_save)
    r.add_get("/api/briefing/faq", h_briefing_faq)
    r.add_post("/api/briefing/faq", h_briefing_faq_add)
    r.add_put("/api/briefing/faq/{faq_id}", h_briefing_faq_update)
    r.add_delete("/api/briefing/faq/{faq_id}", h_briefing_faq_remove)
    r.add_get("/api/prompt-presets", h_prompt_presets)
    r.add_post("/api/prompt-presets", h_prompt_presets_add)
    r.add_put("/api/prompt-presets/{preset_id}", h_prompt_presets_update)
    r.add_delete("/api/prompt-presets/{preset_id}", h_prompt_presets_remove)
    r.add_get("/api/prompter", h_prompter_get)
    r.add_put("/api/prompter", h_prompter_set)
    r.add_get("/api/wsl-reg-stub", h_wsl_reg_stub_get)
    r.add_put("/api/wsl-reg-stub", h_wsl_reg_stub_set)
    r.add_get("/api/prompter/prompts", h_prompter_prompts)
    r.add_get("/api/prompter/contexts", h_prompter_contexts)
    r.add_get("/api/status-checks", h_status_checks)
    r.add_post("/api/status-checks", h_status_checks_add)
    r.add_put("/api/status-checks/{check_id}", h_status_checks_update)
    r.add_delete("/api/status-checks/{check_id}", h_status_checks_remove)
    r.add_get("/api/harnesses", h_harnesses)
    r.add_put("/api/harnesses/{name}/models", h_harness_models_save)
    r.add_get("/api/cflow", h_cflow_runs)
    r.add_get("/api/cflow/run", h_cflow_run_detail)
    r.add_get("/api/cflow/workflows", h_cflow_workflows)
    r.add_get("/api/cflow/definitions", h_cflow_definitions)
    r.add_get("/api/cflow/definition", h_cflow_definition)
    r.add_post("/api/cflow/definition/validate", h_cflow_definition_validate)
    r.add_put("/api/cflow/definition", h_cflow_definition_save)
    r.add_post("/api/cflow/start", h_cflow_start)
    r.add_post("/api/cflow/request", h_cflow_request)
    r.add_post("/api/cflow/request/cancel", h_cflow_request_cancel)
    r.add_post("/api/cflow/skip", h_cflow_skip)
    r.add_post("/api/cflow/archive", h_cflow_archive)
    r.add_post("/api/cflow/approve", h_cflow_approve)
    r.add_post("/api/cflow/state", h_cflow_state_set)
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
    # cflow v2 (docs/cflow-v2.md section 9): its run page, the person's side
    # of a run (a chooser: user choice, a loop at its max) and a read-only
    # definition view. Same token as the v1 approve/select endpoints.
    r.add_get("/api/cflow2/run", h_cflow2_run)
    r.add_get("/api/cflow2/node", h_cflow2_node)
    r.add_get("/api/cflow2/runs", h_cflow2_runs)
    r.add_get("/api/cflow2/rail", h_cflow2_rail)
    r.add_get("/api/cflow2/definition", h_cflow2_definition)
    r.add_post("/api/cflow2/select", h_cflow2_select)
    r.add_post("/api/cflow2/extend", h_cflow2_extend)
    r.add_post("/api/cflow2/approve", h_cflow2_approve)
    r.add_post("/api/cflow2/nudge", h_cflow2_nudge)
    r.add_post("/api/cflow2/abort", h_cflow2_abort)
    r.add_post("/api/cflow2/archive", h_cflow2_archive)
    r.add_post("/api/cflow2/report", h_cflow2_report)
    r.add_post("/api/cflow2/move", h_cflow2_move)
    r.add_post("/api/cflow2/step-out", h_cflow2_step_out)
    r.add_post("/api/cflow2/move/resolve", h_cflow2_move_resolve)
    r.add_get("/api/cflow2/asks", h_cflow2_asks)
    r.add_post("/api/cflow2/collection", h_cflow2_collection)
    r.add_post("/api/cflow2/inputs/check", h_cflow2_inputs_check)
    r.add_get("/api/mesh", h_mesh_list)
    r.add_post("/api/mesh", h_mesh_create)
    r.add_delete("/api/mesh/outgoing/{rid}", h_mesh_outgoing_cancel)
    r.add_get("/api/mesh/{mesh}", h_mesh_get)
    r.add_delete("/api/mesh/{mesh}", h_mesh_delete)
    r.add_post("/api/mesh/{mesh}/members", h_mesh_join)
    r.add_delete("/api/mesh/{mesh}/members/{handle}", h_mesh_leave)
    r.add_patch("/api/mesh/{mesh}/members/{handle}/subroles", h_mesh_member_subroles)
    r.add_patch("/api/mesh/{mesh}/members/{handle}/roles", h_mesh_member_roles)
    r.add_post("/api/mesh/{mesh}/messages", h_mesh_send)
    r.add_post("/api/mesh/{mesh}/urgent", h_mesh_urgent)
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
    # Daemon attach (docs/mesh-design.md "Daemon attach"): the daemon joins
    # a remote mesh with no member; the owner publishes or offers it.
    r.add_post("/api/mesh/{mesh}/attach", h_mesh_attach)
    r.add_delete("/api/mesh/{mesh}/attach", h_mesh_detach)
    r.add_put("/api/mesh/{mesh}/visibility", h_mesh_visibility)
    r.add_put("/api/mesh/{mesh}/project", h_mesh_project)
    r.add_post("/api/mesh/{mesh}/offers", h_mesh_offer)
    r.add_delete("/api/mesh/{mesh}/offers/{machine}", h_mesh_offer_cancel)
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
    # Repository sync across machines (reposync, claunch-dh73y): what a peer's
    # registered workspaces look like (remote address keys and branch tips,
    # read-only) and the approval gate a sync that pushes waits behind.
    r.add_get("/api/relay/peers/{machine}/repos", h_relay_peer_repos)
    r.add_get("/api/reposync/repos", h_reposync_repos)
    r.add_get("/api/reposync/requests", h_reposync_requests_list)
    r.add_post("/api/reposync/requests", h_reposync_request_submit)
    r.add_get("/api/reposync/requests/{rid}", h_reposync_request_get)
    r.add_post("/api/reposync/requests/{rid}/approve", h_reposync_request_approve)
    r.add_post("/api/reposync/requests/{rid}/reject", h_reposync_request_reject)
    # The Workspaces page's Sync panel (claunch-tqm2e). Its pushes wait
    # behind the gate above like a session's do: the button files the card,
    # a person approves it, the panel's next call runs it.
    r.add_get("/api/reposync/workspaces/summary", h_reposync_ws_summary)
    r.add_get("/api/reposync/workspaces/{name}/status", h_reposync_ws_status)
    r.add_post("/api/reposync/workspaces/{name}/agent-preview", h_reposync_ws_agent_preview)
    r.add_post("/api/reposync/workspaces/{name}/sync", h_reposync_ws_sync)
    r.add_post("/api/reposync/workspaces/{name}/branch-push", h_reposync_ws_branch_push)
    r.add_post("/api/reposync/workspaces/{name}/branch-pull", h_reposync_ws_branch_pull)
    r.add_get("/api/relay/meshes", h_relay_meshes)
    # A relay daemon was renamed: rewrite every mesh reference to it here.
    r.add_post("/api/relay/peers/{machine}/rename", h_relay_peer_rename)
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
    # Daemon attach: the public meshes this daemon owns (one hop — never a
    # mirror), an owner's offer pushed to us and the question we ask the
    # owner back before storing it (the relay does not say who sent it),
    # and a guest detaching itself (link-token authenticated).
    r.add_post("/peer/meshes", h_peer_meshes)
    r.add_post("/peer/mesh/offer", h_peer_mesh_offer)
    r.add_post("/peer/mesh/offer/check", h_peer_mesh_offer_check)
    r.add_post("/peer/mesh/detach", h_peer_detach)
    # A linked peer announces its new relay name (link-token authenticated).
    r.add_post("/peer/mesh/renamed", h_peer_renamed)
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
    # Same trust as /peer/sessions (any backend on a relay this daemon is
    # registered with): workspace names, remote address KEYS (never URLs, so
    # no credentials) and branch tips. Read-only; nothing is fetched.
    r.add_post("/peer/repos", h_peer_repos)
    # Remote-shadow sessions (daemon/shadow.py): a linked peer's operator
    # LOOKS at this daemon's members of the mesh that link belongs to -- a
    # card, an output-only terminal, the session line, the transcript.
    # Link-token authenticated and member-scoped; nothing else a local card
    # can do (kill, pause, note, raw keys, resize, paste-image) has a peer
    # route.
    r.add_post("/peer/shadow/cards", h_peer_shadow_cards)
    r.add_post("/peer/shadow/stream", h_peer_shadow_stream)
    r.add_post("/peer/shadow/line", h_peer_shadow_line)
    r.add_post("/peer/shadow/transcript", h_peer_shadow_transcript)
    # ...and this daemon's side of it: the other daemons' members, kept out
    # of /api/sessions (keyed by bare name) and addressed by machine too.
    r.add_get("/api/shadows", h_shadows_list)
    r.add_get("/api/shadows/{machine}/{session}/ws", h_shadow_ws)
    r.add_post("/api/shadows/{machine}/{session}/line", h_shadow_line)
    r.add_get("/api/shadows/{machine}/{session}/transcript", h_shadow_transcript)
    r.add_get("/api/sessions", h_sessions_list)
    r.add_get("/api/transcripts", h_transcripts_list)
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
    r.add_get("/api/sessions/{name}/stats", h_session_stats)
    r.add_get("/api/stats/compare", h_stats_compare)
    r.add_get("/api/sessions/{name}/input-journal", h_session_input_journal)
    r.add_delete("/api/sessions/{name}/input-journal/{request_id}",
                 h_session_input_cancel)
    r.add_get("/api/sessions/{name}/status-checks", h_session_status_checks)
    r.add_post("/api/sessions/{name}/status-checks/reports", h_session_status_checks_report)
    r.add_post("/api/sessions/{name}/status-checks/refresh", h_session_status_checks_refresh)
    r.add_get("/api/sessions/{name}/briefing", h_session_briefing)
    r.add_get("/api/sessions/{name}/queued", h_session_queued)
    r.add_get("/api/sessions/{name}/window", h_session_window)
    r.add_get("/api/sessions/{name}/timers", h_session_timers)
    r.add_get("/api/sessions/{name}/procs", h_session_procs)
    r.add_post("/api/sessions/{name}/procs/{pid}/kill", h_session_proc_kill)
    r.add_post("/api/sessions/{name}/queued/flush", h_session_queued_flush)
    r.add_post("/api/sessions/{name}/queued/hold", h_session_hold)
    r.add_post("/api/sessions/{name}/mcp-ready", h_session_mcp_ready)
    r.add_get("/api/sessions/{name}/reminder", h_session_reminder)
    r.add_get("/api/score-goal/defaults", h_score_goal_defaults)
    r.add_put("/api/score-goal/defaults", h_score_goal_defaults_set)
    r.add_get("/api/role-workflows", h_role_workflows)
    r.add_put("/api/role-workflows", h_role_workflows_set)
    r.add_get("/api/sessions/{name}/score-goal", h_session_score_goal)
    r.add_post("/api/sessions/{name}/reminder", h_session_reminder_set)
    r.add_post("/api/sessions/{name}/reminder/skip", h_session_reminder_skip)
    r.add_post("/api/sessions/{name}/reminder/note", h_session_reminder_note)
    r.add_get("/api/sessions/{name}/children", h_session_children)
    r.add_post("/api/sessions/{name}/children", h_session_spawn)
    r.add_post("/api/sessions/{name}/children/{child}/kill", h_session_child_kill)
    r.add_post("/api/sessions/{name}/parent", h_session_reparent)
    r.add_post("/api/sessions/{name}/detach", h_session_detach)
    r.add_post("/api/sessions/{name}/kill", h_session_kill)
    # A copy of this session's conversation to work in, and the way back
    # (daemon/handoff.py). The merge and the general handoff share a route:
    # what differs is who the target may be, not what happens after.
    r.add_post("/api/sessions/{name}/quick-fork", h_session_quick_fork)
    r.add_post("/api/sessions/{name}/handoff", h_session_handoff)
    r.add_delete("/api/sessions/{name}/handoff", h_session_handoff_cancel)
    r.add_post("/api/sessions/{name}/self-merge", h_session_self_merge)
    r.add_post("/api/sessions/{name}/pause", h_session_pause)
    r.add_post("/api/sessions/{name}/hold", h_session_idle_hold)
    r.add_post("/api/sessions/{name}/wake", h_session_wake)
    r.add_post("/api/sessions/{name}/archive", h_session_archive)
    r.add_delete("/api/sessions/{name}", h_session_delete)
    r.add_post("/api/sessions/{name}/keep-alive", h_session_keep_alive)
    r.add_post("/api/sessions/{name}/note", h_session_note)
    r.add_post("/api/sessions/{name}/cflow-declined", h_session_cflow_declined)
    r.add_delete("/api/sessions/{name}/cflow-declined", h_session_cflow_declined)
    r.add_get("/api/sessions/{name}/progress", h_session_progress)
    r.add_post("/api/sessions/{name}/progress", h_session_progress_set)
    r.add_delete("/api/sessions/{name}/progress", h_session_progress_clear)
    r.add_post("/api/sessions/{name}/model", h_session_model)
    r.add_post("/api/sessions/{name}/respawn", h_session_respawn)
    r.add_post("/api/sessions/{name}/migrate", h_session_migrate)
    r.add_post("/api/sessions/{name}/reborrow", h_session_reborrow)
    r.add_post(
        "/api/sessions/{name}/skip-permissions", h_session_skip_permissions
    )
    r.add_post("/api/sessions/{name}/auto-compact", h_session_auto_compact)
    r.add_post("/api/sessions/{name}/runtime-mode", h_session_runtime_mode)
    r.add_post("/api/sessions/{name}/keys", h_session_keys)
    r.add_post("/api/sessions/{name}/paste-image", h_session_paste_image)
    r.add_post("/api/opening-images", h_opening_image_upload)
    r.add_post("/api/sessions/{name}/deliver", h_session_deliver)
    # The PR wizard (prflow.py): what the session's directory would push,
    # and the push itself. GET is the preview the form opens on, POST does
    # it. Neither touches the session's checkout.
    r.add_get("/api/sessions/{name}/pr/preview", h_session_pr_preview)
    r.add_post("/api/sessions/{name}/pr", h_session_pr)
    r.add_post("/api/sessions/{name}/notice", h_session_notice)
    # The session line's sync mode and its shared text (daemon/line_sync.py).
    # Keystrokes ride the terminal socket; this is the door for the mode
    # switch, for a page reading the state as it attaches, and for a writer
    # with no socket open.
    r.add_get("/api/sessions/{name}/line-sync", h_session_line_sync)
    r.add_post("/api/sessions/{name}/line-sync", h_session_line_sync)
    # One composition, two verbs: GET hands the text to whoever will read it
    # into context (the SessionStart hook, the MCP tool, the CLI); POST types
    # it into the terminal — the operator's push for a session that does not
    # know it needs one.
    r.add_get("/api/sessions/{name}/rebrief", h_session_rebrief)
    r.add_get("/api/sessions/{name}/retrospective", h_session_retrospective)
    r.add_post("/api/sessions/{name}/retrospective", h_session_retrospective)
    r.add_post("/api/sessions/{name}/rebrief", h_session_rebrief)
    # The harness telling the daemon which conversation the session is on
    # now — claude's SessionStart hook after /clear mints a new session id
    # (see SessionManager.repin_conversation).
    r.add_post("/api/sessions/{name}/conversation", h_session_conversation)
    r.add_get("/api/sessions/{name}/loops", h_session_loops)
    r.add_post("/api/sessions/{name}/loops", h_session_loop_add)
    r.add_post("/api/sessions/{name}/loops/{loop}/close", h_session_loop_close)
    r.add_get("/api/sessions/{name}/capture", h_session_capture)
    r.add_get("/api/sessions/{name}/transcript", h_session_transcript)
    r.add_get("/api/sessions/{name}/transcript/info", h_session_transcript_info)
    r.add_get("/api/sessions/{name}/transcript/search", h_session_transcript_search)
    r.add_get("/api/sessions/{name}/wait", h_session_wait)
    r.add_post("/api/sessions/{name}/resize", h_session_resize)
    r.add_get("/api/sessions/{name}/ws", ws_mod.terminal_ws)
    # The CLI tab's raw shell — one socket per viewer, one child under it.
    r.add_get("/api/cli/ws", ws_mod.cli_ws)
    # The board: every repository board the fleet touches, one issue in
    # full, and a session's own slice of it (GET), with the one write the
    # dashboard offers — an issue for a session that has none (POST).
    r.add_get("/api/beads", h_beads_fleet)
    # File an issue on a board without a session and without a shell: the
    # Beads page's own form. Registered before the "{id}" routes for the
    # same reason the literal GETs below are.
    r.add_post("/api/beads", h_beads_create)
    # The boards a create may be filed on, and the workspaces it may record.
    r.add_get("/api/beads/boards", h_beads_boards)
    # Which database each board is, and the one place that is decided: the
    # Settings page's Beads boards card. Registered before "/api/beads/{id}"
    # for the same reason the literal GETs below it are.
    r.add_get("/api/beads/settings", h_beads_settings)
    r.add_put("/api/beads/settings/{board}", h_beads_settings_set)
    r.add_post("/api/beads/settings/{board}/init", h_beads_settings_init)
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
    r.add_get("/api/search/stream", h_search_stream)
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


async def _stop_stats_worker(app: web.Application) -> None:
    await app["stats_worker"].close()


async def _start_loop_lag(app: web.Application) -> None:
    app["loop_lag"].start()


async def _stop_loop_lag(app: web.Application) -> None:
    await app["loop_lag"].stop()


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
        request.app["cookie_name"], session_id,
        httponly=True, samesite="Strict", path="/",
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
    statuses: dict = {}
    for raw in paths:
        if not isinstance(raw, str) or not raw.startswith("/api/"):
            errors[str(raw)] = "only this daemon's /api/ paths"
            continue
        if raw.split("?", 1)[0] == "/api/batch":
            errors[raw] = "a batch cannot carry a batch"
            continue
        try:
            answers[raw] = await _batch_one(template, raw)
        except _ReadRefused as exc:
            errors[raw] = exc.message
            statuses[raw] = exc.status
        except web.HTTPException as exc:
            errors[raw] = f"{exc.status} {exc.reason}"
            statuses[raw] = exc.status
        except Exception as exc:  # noqa: BLE001 -- one bad read, eight good
            log.debug("batch read %s failed", raw, exc_info=True)
            errors[raw] = repr(exc)
    return json_text_response(
        _with_answers({"errors": errors, "statuses": statuses}, answers))


class _ReadRefused(Exception):
    """A carried read whose route answered with an error status.

    Carries the route's own status and ``error`` text so the page can treat a
    batched read exactly like a direct one: a 400 or 404 means something
    different from a 502 to the briefing card, and the route's message is
    what the person needs to see. Raising a bare ``web.HTTPException`` here
    lost both -- its ``status`` is -1, so a failed briefing read as
    "-1 502" (claunch-authx).
    """

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


def _refusal_message(response) -> str:
    """The ``error`` field of a :func:`json_error` body, else the status."""
    status = getattr(response, "status", 0)
    payload = getattr(response, "body", None)
    try:
        body = json.loads(bytes(payload).decode("utf-8")) if payload else None
    except (TypeError, ValueError):
        body = None
    if isinstance(body, dict) and isinstance(body.get("error"), str) and body["error"]:
        return body["error"]
    return f"HTTP {status}"


async def _batch_one(template: web.Request, path: str):
    """Run one of this app's own GET routes and hand back its JSON.

    The route is resolved through the same router that serves it normally,
    so a batched read and a direct one are the same code answering: there is
    no second copy of any payload to drift.

    A JSON body comes back as the route wrote it, as :class:`JSONText`, and
    the reply splices it in rather than parsing and re-encoding it
    (claunch-xrrn0). That trusts the route's ``application/json`` to be
    JSON, which holds for :func:`json_response` and aiohttp's own, the two
    ways a route here writes one. Any other body is parsed, as before.
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
    status = getattr(response, "status", 200)
    if status >= 400:
        raise _ReadRefused(status, _refusal_message(response))
    payload = getattr(response, "body", None)
    if payload is None:
        return None
    text = bytes(payload).decode("utf-8")
    if getattr(response, "content_type", None) == "application/json":
        return JSONText(text)
    return json.loads(text)


#: How long a control socket waits between heartbeats. Same value the
#: terminal sockets use, and for the same reason -- see ws.HEARTBEAT.
CONTROL_HEARTBEAT = 60

#: How many reads this socket will serve at once. Reads run off the receive
#: loop (see :func:`h_control_ws`), so without a ceiling a client could make
#: one connection hold an arbitrary number of router dispatches open. The
#: page has at most a tick's batch and a few widget polls in flight.
CONTROL_READS_IN_FLIGHT = 8


async def h_control_ws(request: web.Request) -> web.WebSocketResponse:
    """The page's reads, on a connection it already holds.

    ``/api/batch`` put the dashboard's tick on one connection instead of
    nine, which was not enough. A browser keeps a connection in its pool
    after the answer arrives, so the page still held one for the poll, one
    for the liveness probe and one for each terminal socket -- five against
    Firefox's ceiling of six. One more tab, and a terminal's upgrade sat in
    the connection queue until an idle pooled connection timed out, roughly
    two minutes later (claunch-riq5).

    A socket does not have that shape. It is one connection for as long as
    the page is open, every read of every tick rides it, and the count stops
    depending on what the person clicks. The reads themselves are unchanged:
    each frame names paths, and :func:`_batch_one` answers them through the
    same router that serves them over HTTP, so there is no second copy of
    any payload to drift.

    Frames, client to server:

    - ``{"type":"read","id":N,"paths":[...]}`` -> ``{"type":"read_result",
      "id":N,"answers":{...},"errors":{...}}``. ``id`` is the client's, echoed
      back, because several reads may be in flight on one socket -- and they
      are answered as each finishes, which is not the order they arrived in.
    - ``{"type":"link_failed", ...}`` -> recorded, not answered. What the page
      reports when it could not open a terminal socket at all; see
      :meth:`connections.Registry.link_failed`.
    - ``{"type":"ping"}`` -> ``{"type":"pong"}``. A ping carrying ``t``
      gets it back, with the relay status beside it -- see
      :func:`_control_pong`.
    - A read carrying ``"parts": true`` may be answered in ``read_part``
      frames; the page acknowledges each with ``{"type":"read_ack","id":N,
      "seq":K}`` -> not answered. See :class:`channel.Carrier`.

    The socket being open is itself the liveness answer, which is what
    retires the probe the page used to spend a connection on.
    """
    # Auth already happened: /api/ prefix, so the shared middleware validated
    # a Bearer header or the session cookie.
    template = request.clone(method="GET")
    ws = web.WebSocketResponse(heartbeat=CONTROL_HEARTBEAT)
    await ws.prepare(request)
    request.app["websockets"].add(ws)
    conns = connections.install(request.app)
    record = conns.opened("control", "(control)", request, ws=ws)
    carrier = channel.Carrier(ws, request.app, request)
    reads: set[asyncio.Task] = set()
    # P2P signalling (claunch-mhzt4): offered only on a socket that is not
    # itself a P2P bridge, and only when the daemon has a hub.
    hub = request.app.get("p2p")
    if hub is not None and p2p.via_p2p(request, hub):
        hub = None
    owner = f"c{id(ws):x}"
    negotiating: set[asyncio.Task] = set()

    async def p2p_send(frame: dict) -> None:
        await carrier.send_soon(json.dumps(frame))

    async def answer_read(frame: dict) -> None:
        try:
            answers, errors, statuses = await _control_read(
                template, frame.get("paths")
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 -- one read, not the socket
            log.debug("control read failed", exc_info=True)
            answers, errors, statuses = {}, {"": repr(exc)}, {}
        # The bulk lane: a pong or a terminal's output queued after this goes
        # ahead of it, and a page that takes parts gets it in parts
        # (claunch-iss86, see channel.Carrier).
        # Encoded around the routes' own text, not parsed and re-encoded
        # (claunch-xrrn0): this runs on the loop every terminal waits on.
        await carrier.send_bulk(_with_answers({
            "type": "read_result",
            "id": frame.get("id"),
            "errors": errors,
            "statuses": statuses,
        }, answers), parts=frame.get("parts") is True, key=frame.get("id"))

    try:
        init = {"type": "init"}
        if hub is not None and p2p.available():
            init["p2p"] = hub.browser_info()
        await carrier.send_str(json.dumps(init))
        async for msg in ws:
            if msg.type == WSMsgType.BINARY:
                carrier.deliver_binary(msg.data)
                continue
            if msg.type != WSMsgType.TEXT:
                if msg.type in (WSMsgType.CLOSE, WSMsgType.ERROR):
                    break
                continue
            received = time.monotonic()
            try:
                frame = json.loads(msg.data)
            except (ValueError, TypeError):
                continue
            if not isinstance(frame, dict):
                continue
            if await carrier.deliver_text(frame):
                continue
            kind = frame.get("type")
            if kind == "ping":
                # Queued, not awaited: this loop is the lane every
                # terminal's keystrokes arrive in, and a page that has
                # stopped draining parks a write for as long as it
                # stays stopped (test_control_write_offloop).
                # Built as it is written, so the time it spent here -- the
                # queue included -- is measured, not guessed (claunch-iss86).
                await carrier.send_soon(functools.partial(
                    _control_pong_text, request, frame, received))
            elif kind == "read_ack":
                carrier.ack(frame)
            elif kind == "link_failed":
                conns.link_failed(request, frame)
            elif kind in ("p2p_offer", "p2p_ice", "p2p_bye"):
                if hub is None:
                    if kind == "p2p_offer":
                        await p2p_send({"type": "p2p_error", "id": frame.get("id"),
                                        "error": "p2p unavailable"})
                    continue
                if kind == "p2p_offer":
                    # Answering gathers candidates (about a second with
                    # STUN), so it runs beside this loop like a read does.
                    task = asyncio.ensure_future(hub.offer(owner, frame, p2p_send))
                    negotiating.add(task)
                    task.add_done_callback(negotiating.discard)
                elif kind == "p2p_ice":
                    await hub.ice(owner, frame)
                else:
                    await hub.bye(owner, frame)
            elif kind == "read":
                # Off this loop, on a task of its own. A read is a router
                # dispatch per path and some of them are slow -- /api/mesh
                # answered 1.7MB in 3.3s on this machine, 2026-09-20 -- and
                # the loop is now also the lane every terminal's keystrokes
                # arrive in. Awaited here, one read stopped this socket from
                # reading anything for its whole duration: nothing typed in
                # any terminal reached a PTY, and no resize or repaint got
                # through, which is a terminal that paints once and then sits
                # there (claunch-gh4f made the terminals share this socket;
                # the reads were already on it from claunch-riq5).
                if len(reads) >= CONTROL_READS_IN_FLIGHT:
                    await carrier.send_soon(json.dumps({
                        "type": "read_result",
                        "id": frame.get("id"),
                        "answers": {},
                        "errors": {"": "too many reads in flight"},
                    }))
                    continue
                task = asyncio.ensure_future(answer_read(frame))
                reads.add(task)
                task.add_done_callback(reads.discard)
    except ConnectionResetError:
        pass
    finally:
        for task in list(reads) + list(negotiating):
            task.cancel()
        if hub is not None:
            await hub.socket_gone(owner)
        await carrier.shutdown(ws.exception())
        request.app["websockets"].discard(ws)
        if not ws.closed:
            await ws.close()
        conns.closed(record, ws.close_code, ws.exception())
    return ws


def _control_pong(request: web.Request, frame: dict) -> dict:
    """The answer to a control-socket ping.

    A bare ping gets a bare pong: that is the liveness answer older pages
    wait for. A ping that carries ``t`` (the page's own clock, opaque here)
    is a latency probe (claunch-8ufey): ``t`` comes back untouched so the
    page measures its own round trip -- through whatever sits between, a
    relay tunnel included, and through this socket's send queue, which is
    the delay a person actually waits on. The relay status rides along,
    because that is where the uplink's round trip to the relay lives and
    the page would otherwise need another read to show the two together.
    So does the event loop's own lag (claunch-y9ax9): the page's round trip
    includes however long the loop was busy before it read the ping, and
    only the daemon can say how much of it that was.
    """
    if "t" not in frame:
        return {"type": "pong"}
    pong = {
        "type": "pong",
        "t": frame.get("t"),
        "relay": request.app["relay_state"](),
        "loop": request.app["loop_lag"].snapshot(),
    }
    # Carried over a DataChannel (claunch-mhzt4): the relay's round trip is
    # not on this socket's path, and the page must not subtract it.
    if p2p.via_p2p(request, request.app.get("p2p")):
        pong["via"] = "p2p"
    return pong


def _control_pong_text(request: web.Request, frame: dict, received: float) -> str:
    """:func:`_control_pong` as the writer puts it on the socket, with
    ``daemon_ms``: how long the ping was in this daemon, from its frame being
    read to its answer being written -- the socket's queue included. The page
    subtracts it, and the relay's round trip, from its own round trip to show
    what is left for the path between the browser and the relay
    (claunch-iss86)."""
    pong = _control_pong(request, frame)
    if "t" in pong:
        pong["daemon_ms"] = round((time.monotonic() - received) * 1000.0, 1)
    return json.dumps(pong)


async def _control_read(template: web.Request, paths) -> tuple[dict, dict, dict]:
    """The read half of :func:`h_control_ws`, with the same rules as a batch.

    Kept beside the socket rather than shared with :func:`h_batch` at the
    top: what the two have in common is :func:`_batch_one`, and that is
    already the part where a payload could drift. The validation above it is
    six lines that read better twice than they would behind a third name.
    """
    answers: dict = {}
    errors: dict = {}
    statuses: dict = {}
    if not isinstance(paths, list) or not paths:
        return answers, {"": "paths must be a non-empty list"}, statuses
    if len(paths) > BATCH_MAX:
        return answers, {"": f"at most {BATCH_MAX} paths per read"}, statuses
    for raw in paths:
        if not isinstance(raw, str) or not raw.startswith("/api/"):
            errors[str(raw)] = "only this daemon's /api/ paths"
            continue
        try:
            answers[raw] = await _batch_one(template, raw)
        except _ReadRefused as exc:
            errors[raw] = exc.message
            statuses[raw] = exc.status
        except web.HTTPException as exc:
            errors[raw] = f"{exc.status} {exc.reason}"
            statuses[raw] = exc.status
        except Exception as exc:  # noqa: BLE001 -- one bad read, eight good
            log.debug("control read %s failed", raw, exc_info=True)
            errors[raw] = repr(exc)
    return answers, errors, statuses


async def h_connections(request: web.Request) -> web.Response:
    """Every socket the daemon holds open, and the last hundred that closed.

    Authenticated like the rest of ``/api/``: this names sessions and remote
    addresses, which is exactly what the open endpoint above must not.
    """
    registry = connections.install(request.app)
    return json_response(
        registry.snapshot(
            connections.http_connection_count(request.app),
            connections.live_peer_ports(request.app),
        )
    )


async def h_shared_reads(request: web.Request) -> web.Response:
    """What the shared reads did since the daemon started (claunch-jkvpv).

    Per wrapped route: how many reads ran it (``computes``), were answered
    from a stored answer (``hits``) or waited on one being built
    (``joins``), and which accepted writes dropped its answers. Two readings
    a minute apart give the rates the live measurement compares across tab
    counts; see daemon/shared_reads.py.
    """
    return json_response(request.app["shared_reads"].stats())


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

    def gone() -> bool:
        # aiohttp keeps running a handler whose client disconnected, so the
        # wait asks the transport instead of expecting a cancellation
        # (claunch-zawnr).
        transport = request.transport
        return transport is None or transport.is_closing()

    result = await request.app["window"].acquire(
        cls,
        session=body.get("session"),
        pid=int(body.get("pid") or 0),
        label=str(body.get("label") or ""),
        wait=float(body.get("wait") or 0),
        workers=int(body.get("workers") or 0),
        priority=body.get("priority", 0),
        force=bool(body.get("force")),
        # A pid is only this machine's to check from a loopback caller.
        bound=bool(body.get("bound")) and request.remote in ("127.0.0.1", "::1"),
        gone=gone,
    )
    if result.get("error"):
        return json_response(result, status=400)
    return json_response(result)


async def h_window_prioritize(request: web.Request) -> web.Response:
    """Operator override: set a waiting request's priority (claunch-8kald).

    ``priority`` omitted moves the request to the top. The CLI refuses this
    inside a managed session; the daemon, as with restart, does not tell
    callers apart.
    """
    body = await _json_body(request)
    grant_id = body.get("grant_id")
    if not grant_id:
        return json_response({"error": "prioritize wants a grant_id"}, status=400)
    raw = body.get("priority")
    try:
        priority = None if raw is None or raw == "" else int(raw)
    except (TypeError, ValueError):
        return json_response({"error": f"priority must be an integer, not {raw!r}"}, status=400)
    result = request.app["window"].prioritize(str(grant_id), priority)
    if result is None:
        return json_response({"error": f"no waiting request {grant_id}"}, status=404)
    return json_response(result)


async def h_window_report(request: web.Request) -> web.Response:
    """Attach a test run's result to a held grant (claunch-8kald).

    ``tests/conftest.py`` calls this when pytest finishes, for its own grant
    or for the parent's grant it ran under; the result reaches the run
    history when the grant is released.
    """
    body = await _json_body(request)
    grant_id = body.get("grant_id")
    if not grant_id:
        return json_response({"error": "report wants a grant_id"}, status=400)
    ok = request.app["window"].report_result(str(grant_id), body.get("result"))
    if not ok:
        return json_response(
            {"reported": False, "error": f"no held grant {grant_id} or no usable result"},
            status=404,
        )
    return json_response({"reported": True})


async def h_window_history(request: web.Request) -> web.Response:
    """The window's run log and statistics. Kept 7 days, then dropped."""
    q = request.query
    try:
        days = float(q["days"]) if q.get("days") else None
        limit = int(q.get("limit") or 50)
    except ValueError:
        return json_response({"error": "days and limit must be numbers"}, status=400)
    return json_response(
        request.app["window"].history(
            days=days, cls=q.get("class") or None, session=q.get("session") or None,
            limit=limit,
        )
    )


async def h_window_limits(request: web.Request) -> web.Response:
    """The test window's limits: what is in force and who may change them."""
    return json_response(request.app["window"].status()["limits_control"])


async def h_window_set_limits(request: web.Request) -> web.Response:
    """Change the test window's limits (claunch-k7x63).

    Body ``{"limits": {name: value | null}, "by": "<who>"}``; ``null`` clears
    an override back to the config default. Only the daemon that owns the
    limits accepts it -- the main daemon on a machine window. A daemon that
    shares the main one's window answers 409 and names where to set them.
    """
    body = await _json_body(request)
    changes = body.get("limits")
    try:
        view = request.app["window"].set_limits(
            changes if isinstance(changes, dict) else {},
            by=str(body.get("by") or "").strip()[:80],
        )
    except window_mod.LimitsNotEditable as exc:
        return json_response({"error": str(exc), "editable": False}, status=409)
    except ValueError as exc:
        return json_response({"error": str(exc)}, status=400)
    return json_response(view)


async def h_window_force(request: web.Request) -> web.Response:
    """Operator override: grant a waiting request now, past every limit."""
    body = await _json_body(request)
    grant_id = body.get("grant_id")
    if not grant_id:
        return json_response({"error": "force wants a grant_id"}, status=400)
    holder = request.app["window"].force(str(grant_id))
    if holder is None:
        return json_response({"error": f"no waiting request {grant_id}"}, status=404)
    return json_response({"forced": True, "holder": holder})


async def h_window_release(request: web.Request) -> web.Response:
    """Hand a grant back. ``grant_id`` picks one; ``session`` releases all of
    that session's, which is how a caller that lost its grant id (a compacted
    context) can still do the honest thing."""
    body = await _json_body(request)
    window = request.app["window"]
    grant_id = body.get("grant_id")
    if grant_id:
        if body.get("result") is not None:
            window.report_result(str(grant_id), body.get("result"))
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
            "loop": request.app["loop_lag"].snapshot(),
            # The PTY host and whether it runs the daemon's code
            # (claunch-xnnct.3; daemon/host_code.py).
            "pty_host": host_code.info(),
        }
    )


async def h_daemon_restart_check(request: web.Request) -> web.Response:
    """Whether a keep restart now would leave the PTY host on older code
    (claunch-xnnct.3). The CLI asks before it restarts or files a gate
    request, so it can refuse with the files and the two ways forward
    instead of failing on the 409 below. Read-only."""
    check = host_code.keep_check()
    body = dict(check)
    if check["stale"]:
        body["message"] = host_code.refusal(check)
        body["hint"] = host_code.HINT
    return json_response(body)


def _stale_keep_refusal(body: dict) -> Optional[web.Response]:
    """The 409 for a keep restart onto a stale host, or None to go ahead.

    ``keep_stale: true`` in the body is the explicit "keep it anyway"; the
    check is skipped for it, and for every mode but keep."""
    if body.get("keep_stale") is True:
        return None
    check = host_code.keep_check()
    if not check["stale"]:
        return None
    return web.json_response(
        {"error": host_code.refusal(check), "stale": check, "hint": host_code.HINT},
        status=409,
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
    body = await _json_body(request)
    mode = body.get("mode") or "stop"
    if mode not in SHUTDOWN_MODES:
        return json_error(400, f"mode must be one of {', '.join(SHUTDOWN_MODES)}")
    if mode == "keep":
        refused = _stale_keep_refusal(body)
        if refused is not None:
            return refused
    # A CLI restart is this call followed by a fresh start (daemon_client.
    # restart): "keep" is how it says so, "stop" (and no body) is a stop.
    request.app["restart_mode"] = mode
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, request.app["shutdown_event"].set)
    return json_response({"ok": True, "mode": mode})


#: What ``/api/daemon/shutdown`` accepts as ``mode`` (claunch-xnnct.1).
SHUTDOWN_MODES = ("keep", "full", "stop")


async def h_daemon_restart(request: web.Request) -> web.Response:
    """Shut down and hand this port to a fresh daemon process.

    Identical to shutdown from in here — same event, same teardown, same
    notice to attached clients — plus one bit of intent that ``__main__``
    reads once the loop has drained. The successor is spawned there, after
    the singleton lock is released, so it finds the lock free instead of
    spending its grace window waiting this process out. Sessions come back
    the way they do on any restart: relaunched per their ``restore`` flag --
    or, with the PTY host on, still running and adopted by the successor
    (body ``{"mode": "keep"}``, the default); ``"full"`` ends them and the
    host, and they are relaunched (claunch-xnnct.1).

    It records itself first, exactly as the CLI door does. The API cannot
    name a caller -- an HTTP request carries no session -- so this record
    owes nobody a notice; what it does is stop the successor from reporting
    this boot as one that nothing asked for.
    """
    body = await _json_body(request)
    mode = body.get("mode") or "keep"
    if mode not in restart_gate.PTY_HOST_MODES:
        return json_error(400, "mode must be keep or full")
    if mode == "keep":
        refused = _stale_keep_refusal(body)
        if refused is not None:
            return refused
    request.app["restart_requested"] = True
    request.app["restart_mode"] = mode
    restart_notice.record_request(via="api")
    loop = asyncio.get_running_loop()
    loop.call_later(0.1, request.app["shutdown_event"].set)
    return json_response({"ok": True, "restarting": True, "mode": mode})


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
    was an agent asking. ``reason`` (optional) is why the restart is needed,
    shown in full on the web UI's card. ``GateBusy`` (a request already
    pending) escapes to the middleware and comes back as 409.
    """
    body = await _json_body(request)
    session = str(body.get("session") or "").strip()
    if not session:
        return json_error(400, "session is required")
    reason = body.get("reason")
    if reason is not None and not isinstance(reason, str):
        return json_error(400, "reason must be a string")
    pty_host = body.get("pty_host") or "keep"
    if pty_host not in restart_gate.PTY_HOST_MODES:
        return json_error(400, "pty_host must be keep or full")
    record = request.app["restart_gate"].submit(
        session=session, reason=reason or "", pty_host=pty_host,
        keep_stale=body.get("keep_stale") is True,
    )
    return json_response({"ok": True, "request": record})


async def h_restart_request_approve(request: web.Request) -> web.Response:
    """The web UI's Approve: settle the gate and restart — the same intent
    and event as ``/api/daemon/restart``, minus one difference the gate owns:
    the request is recorded with the asking session's name, so the successor
    daemon owes that session the account of the boot."""
    body = await _json_body(request)
    pty_host = body.get("pty_host")
    if pty_host is not None and pty_host not in restart_gate.PTY_HOST_MODES:
        return json_error(400, "pty_host must be keep or full")
    gate = request.app["restart_gate"]
    try:
        record = gate.approve(decided_by="web", pty_host=pty_host,
                              keep_stale=body.get("keep_stale") is True)
    except restart_gate.StaleHost as exc:
        # The card offers both answers: "Restart with PTY host" sends
        # pty_host full, "Keep anyway" sends keep_stale (claunch-xnnct.3).
        return web.json_response(
            {"error": str(exc), "stale": exc.check, "hint": host_code.HINT}, status=409,
        )
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


async def h_restart_request_extend(request: web.Request) -> web.Response:
    """The web UI's Extend: move the deadline out, settle nothing.

    The gate's default answer belongs to the clock, and this is how somebody
    who is at the page takes it back without having decided: the request
    stays pending, the asking session stays where it was, and both sides read
    the new deadline off the same record. It may be pressed any number of
    times (claunch-mm8zv)."""
    record = request.app["restart_gate"].extend(decided_by="web")
    if record is None:
        return json_error(409, "no pending restart request")
    return json_response({"ok": True, "extended": True, "request": record})


class _EffortSource:
    """What a profile's launches read their reasoning effort from.

    Resolved once per profile and asked once per harness row. ``spec`` is the
    profile's spec with the template under the provider and the profile chain
    over it (:func:`providers.spec_for`, the launch's own resolution);
    ``claude_env`` is the chain's Claude-vocabulary env, which the Claude
    launch applies *after* the provider translation, so a
    ``CLAUDE_CODE_EFFORT_LEVEL`` written there is the one Claude Code reads.
    ``error`` is why either could not be read -- a launch of this profile
    fails on the same error.
    """

    def __init__(self, profile_obj, doc: dict):
        self.spec = None
        self.claude_env: dict = {}
        self.error: Optional[str] = None
        try:
            self.spec = providers.spec_for(profile_obj, doc=doc)
            self.claude_env = runner._claude_profile_env(profile_obj)
        except (
            providers.ProviderError,
            lineage.LineageError,
            runner.RunnerError,
        ) as exc:
            self.spec = None
            self.error = str(exc)

    def row(self, harness_name: str) -> dict:
        """``reasoning_effort`` and ``reasoning_effort_problem`` for one row.

        ``reasoning_effort`` is the value a launch on ``harness_name`` applies,
        None when nothing is configured (the harness's own default decides).
        ``reasoning_effort_problem`` is None unless that launch is refused
        over it: a harness with no translation for the field cannot launch a
        profile that sets one (:func:`runner.harness_translation`), so its row
        carries the configured value *and* says why it will not run, rather
        than reading as "unset".
        """
        if self.spec is None:
            return {"reasoning_effort": None, "reasoning_effort_problem": self.error}
        if harness_name == harness_registry.CLAUDE_HARNESS:
            var = provider_spec.CLAUDE_REASONING_EFFORT
            env = translators.claude(self.spec).env
            if var in self.claude_env:
                env = self.claude_env
            return {
                "reasoning_effort": str(env.get(var) or "").strip() or None,
                "reasoning_effort_problem": None,
            }
        value = self.spec.reasoning_effort or None
        problem = None
        if value and "reasoning_effort" in (
            translators.for_harness(harness_name, self.spec).missing
        ):
            problem = (
                f"harness {harness_name!r} cannot translate configured "
                "provider field(s): reasoning_effort"
            )
        return {"reasoning_effort": value, "reasoning_effort_problem": problem}


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
    shared_declared = store.shared_settings(doc).get(PERMISSION_MODE_KEY)
    override = store.profile_settings(profile_obj.name, doc).get(PERMISSION_MODE_KEY)
    declared = override if override is not None else shared_declared
    value = settings.dotted_get(settings.load(profile_obj), PERMISSION_MODE_KEY)
    target = store.effective_profile_settings(profile_obj.name, doc).get(PERMISSION_MODE_KEY)
    return {
        "key": PERMISSION_MODE_KEY,
        "value": value,
        "target": target,
        "declared": declared,
        "source": "profile" if override is not None else (
            "claunch-default" if declared is None else "declared"
        ),
        "override": override,
        "shared_declared": shared_declared,
        "shared_target": store.effective_shared_settings(doc).get(PERMISSION_MODE_KEY),
        "packaged_default": store.SHARED_SETTINGS_DEFAULTS[PERMISSION_MODE_KEY],
        "converged": value == target,
        "modes": list(settings.PERMISSION_MODES),
    }


#: Pi's TUI mode, a key of pi's own ``settings.json``. claunch does not keep
#: it anywhere new: a profile declares it on the channel every pi settings
#: key already rides (``harness_options.pi.settings``), and the launch merges
#: that into the profile's pi home (``translators`` -> ``runner``). Absent
#: means pi's own default, fullscreen.
PI_TUI_MODE_KEY = "tuiMode"
PI_TUI_MODES = ("regular", "fullscreen")
PI_TUI_MODE_DEFAULT = "fullscreen"


def _runs_pi(profile_obj, default_harness: str, doc: Optional[dict] = None) -> bool:
    """Whether a session of this profile can be launched on pi.

    Either pi is the profile's default harness, or the harness policy allows
    the explicit ``<profile>:pi`` selector -- the common case, since a pi
    profile usually keeps Claude as its default.
    """
    if default_harness == PI_HARNESS:
        return True
    try:
        return bool(harness_policy.evaluate(profile_obj, PI_HARNESS, doc=doc).allowed)
    except (
        harness_policy.HarnessPolicyError,
        lineage.LineageError,
        providers.ProviderError,
    ):
        return False


def _pi_tui_mode(profile_obj, doc: dict, entry) -> dict:
    """What the next pi session of this profile runs its TUI in, and why.

    * ``declared``   -- this profile's own declaration, ``None`` when it has
                        none (the page's selector edits exactly this).
    * ``effective``  -- what the launch writes: the declaration merged over
                        template, provider and parent profiles. ``None`` when
                        no layer declares one, and the launch leaves the key
                        in the file alone.
    * ``file_value`` -- what the profile's pi ``settings.json`` holds now. A
                        hand-written value there survives a launch only while
                        ``effective`` is ``None``.
    * ``next``       -- the mode the next session gets, all of the above
                        resolved; ``source`` says which of them decided it.
    """
    own = store.profile_entry(profile_obj.name, doc).get(provider_spec.HARNESS_OPTIONS_FIELD)
    declared = None
    if isinstance(own, dict):
        block = own.get(PI_HARNESS)
        channel = block.get("settings") if isinstance(block, dict) else None
        if isinstance(channel, dict):
            declared = channel.get(PI_TUI_MODE_KEY)
    error = None
    effective = None
    try:
        declared_settings = providers.spec_for(profile_obj, doc=doc).options(
            PI_HARNESS
        ).get("settings")
        if isinstance(declared_settings, dict):
            effective = declared_settings.get(PI_TUI_MODE_KEY)
    except (providers.ProviderError, lineage.LineageError) as exc:
        error = str(exc)
    file_value = None
    if entry is not None:
        path = entry.profile_home(profile_obj.config_dir) / translators.PI_SETTINGS_FILE
        try:
            loaded = json.loads(path.read_text(encoding="utf-8") or "{}")
        except (OSError, ValueError):
            loaded = {}
        if isinstance(loaded, dict):
            file_value = loaded.get(PI_TUI_MODE_KEY)
    if effective is not None:
        source = "profile" if declared is not None else "inherited"
        nxt = effective
    elif file_value is not None:
        source = "file"
        nxt = file_value
    else:
        source = "pi-default"
        nxt = PI_TUI_MODE_DEFAULT
    return {
        "key": PI_TUI_MODE_KEY,
        "declared": declared,
        "effective": effective,
        "file_value": file_value,
        "next": nxt,
        "source": source,
        "default": PI_TUI_MODE_DEFAULT,
        "modes": list(PI_TUI_MODES),
        "error": error,
    }


#: The two rule lists the profiles page reports on, spelled once each for the
#: same reason :data:`PERMISSION_MODE_KEY` is. ``defaultMode`` is one value and
#: the page shows it; these are lists, and the question a person asks about a
#: list is not "what does it hold" but "is the rule claunch plants in it" --
#: so they are reported as presence set against an expectation, and the value
#: rides along only as the evidence for that answer.
PERMISSION_ALLOW_KEY = "permissions.allow"
PERMISSION_DENY_KEY = "permissions.deny"


def _permission_rule_list(data: dict, key: str, expected, declared) -> dict:
    """One rule list, read against what claunch plants in it.

    ``expected`` is the gate guard's half for this key. ``missing`` is the
    whole answer to "is it whole" -- a list rather than a boolean because a
    half-planted guard is the state worth seeing, and the names of the absent
    rules are what makes the page actionable. Counts are read off
    ``expected``/``missing`` by the caller; carrying them here as well would be
    a third spelling of the same fact, free to disagree with the other two.
    """
    found = settings.dotted_get(data, key)
    found = [str(rule) for rule in found] if isinstance(found, list) else []
    missing = [rule for rule in expected if rule not in found]
    return {
        "key": key,
        "value": found,
        "expected": [*expected],
        "missing": missing,
        "declared": declared,
        "converged": not missing,
    }


def _permission_rules(profile_obj, doc: dict) -> dict:
    """Which of the gate guard's rules this profile's settings file is missing.

    The guard has two halves and they fail in opposite directions, which is why
    both are reported and neither is folded into a single "ok":

    * ``allow`` -- the claunch MCP server. Missing it is the failure this
      project spent a round on: Claude Code's ``auto`` classifier reads the
      ``deny`` half, finds the MCP tool that reaches the same commands, and
      refuses the second as circumvention of the first, leaving a run with no
      channel to advance on.
    * ``deny`` -- the four human gate commands, taken off the agent's shell.
      Expect-many (a shell times both spellings of each verb), so it is the one
      that can be *partly* planted, and a count that silently rounds to "has
      some" would hide exactly that.

    ``expected`` is read off :mod:`install` rather than re-spelled here. That
    module is where the guard is defined; a second copy is a second thing to
    forget the day a gate command gains a shell.

    ``declared`` is the shared declaration (``claunch shared permissions.allow``
    and friends), ``None`` when nobody declared one. It is reported beside the
    files rather than instead of them, because the two routes are not the same:
    a declaration reaches profiles through ``claunch apply``, which converges a
    list by *assigning* it, while the guard reaches them through ``claunch
    install``, which merges. A profile can hold the rules with nothing declared
    and vice versa, and the page is the place that says which is in play.
    """
    data = settings.load(profile_obj)
    shared = store.shared_settings(doc)
    allow = _permission_rule_list(
        data, PERMISSION_ALLOW_KEY, install.GATE_ALLOW_RULES,
        shared.get(PERMISSION_ALLOW_KEY))
    deny = _permission_rule_list(
        data, PERMISSION_DENY_KEY, install.GATE_DENY_RULES,
        shared.get(PERMISSION_DENY_KEY))
    return {
        "allow": allow,
        "deny": deny,
        "converged": allow["converged"] and deny["converged"],
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


@shared_reads.shared("profiles")
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
        # The reasoning effort a launch of this profile applies, resolved the
        # way the launch resolves it. The session form shows it as the
        # "profile default" effort: for a harness that declares no
        # ``efforts`` this setting is the only source of the value, and the
        # form cannot resolve the layers itself.
        effort = _EffortSource(p, doc)
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
                    **effort.row(name),
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
                    **effort.row(name),
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
                    # The other half of the same settings file, gated on the
                    # same harness for the same reason: a rule list another
                    # harness never reads has no answer here, and ``None``
                    # says that rather than ``missing: everything``.
                    "permission_rules": (
                        _permission_rules(p, doc)
                        if name == harness_registry.CLAUDE_HARNESS
                        else None
                    ),
                    # A pi settings key: answered for a profile that can run
                    # pi at all, which is usually not its default harness (a
                    # Claude profile launched as ``<p>:pi``). None elsewhere,
                    # for the same reason as above.
                    "pi_tui_mode": (
                        _pi_tui_mode(p, doc, registry.get(PI_HARNESS))
                        if _runs_pi(p, name, doc)
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
                        **effort.row(harness_name),
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
                    **effort.row(harness_name),
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
            # Profiles whose auto_compact_at exceeds their context_window:
            # a session on one is refused (harness.normalize), and the form
            # says so next to the profile picker before Create is pressed.
            "compact_problems": _compact_problems(doc),
        }
    )


def _compact_problems(doc: dict) -> dict:
    try:
        return providers.compact_problems(doc)
    except (providers.ProviderError, store.StoreError):
        return {}


async def h_profiles_pi_tui_mode(request: web.Request) -> web.Response:
    """Declare (or, with ``mode: null``, undeclare) one pi profile's TUI mode.

    Writes ``profiles.<profile>.harness_options.pi.settings.tuiMode`` and
    nothing else: the next pi session of that profile gets it merged into its
    ``settings.json`` at launch, and a session already running keeps what it
    started with. Undeclaring prunes the containers it leaves empty, so the
    config reads as if the page had never touched it.
    """
    body = await _json_body(request)
    name = body.get("profile")
    if not isinstance(name, str) or not name.strip():
        return json_error(400, "profile must be a non-empty profile name")
    try:
        target = profile_mod.require(name)
        if target.name not in {p.name for p in profile_mod.list_all()}:
            return json_error(400, "profile must name an existing profile")
        if not _runs_pi(target, lineage.effective_harness(target)):
            return json_error(
                400, "TUI mode is only available for profiles that can run pi"
            )
    except (profile_mod.ProfileError, lineage.LineageError) as exc:
        return json_error(400, str(exc))
    if "mode" not in body:
        return json_error(400, "mode is required; use null to restore pi's default")
    raw = body.get("mode")
    declared = str(raw).strip() if raw is not None else ""
    if declared and declared not in PI_TUI_MODES:
        return json_error(
            400,
            f"unknown TUI mode {declared!r} (known: {', '.join(PI_TUI_MODES)})",
        )

    def _mutate(doc: dict) -> None:
        section = doc.get("profiles")
        entry = section.get(target.name) if isinstance(section, dict) else None
        if not isinstance(entry, dict):
            if not declared:
                return
            entry = store._writable_entry(doc, target.name)
        path = (provider_spec.HARNESS_OPTIONS_FIELD, PI_HARNESS, "settings")
        if declared:
            node = entry
            for key in path:
                child = node.get(key)
                if not isinstance(child, dict):
                    child = {}
                    node[key] = child
                node = child
            node[PI_TUI_MODE_KEY] = declared
            return
        chain = [entry]
        for key in path:
            child = chain[-1].get(key)
            if not isinstance(child, dict):
                return
            chain.append(child)
        chain[-1].pop(PI_TUI_MODE_KEY, None)
        for parent, key in zip(reversed(chain[:-1]), reversed(path)):
            if parent[key]:
                break
            parent.pop(key)

    doc = store.update(_mutate)
    return json_response(
        {
            "profile": target.name,
            "declared": declared or None,
            "pi_tui_mode": _pi_tui_mode(
                target, doc, harness_registry.registry(doc).get(PI_HARNESS)
            ),
        }
    )


async def h_profiles_permission_mode(request: web.Request) -> web.Response:
    """Save a shared default or a profile override, then apply that scope.

    Without ``profile``, update the shared declaration and converge all Claude
    profiles, preserving their overrides. With ``profile``, update and apply
    only that existing Claude profile. A null/empty mode removes the scoped
    declaration: profiles resume the shared default; shared resumes the
    packaged default. Both paths use the same planner as ``claunch apply``.
    """
    body = await _json_body(request)
    target_profile = None
    if "profile" in body:
        name = body["profile"]
        if not isinstance(name, str) or not name.strip():
            return json_error(400, "profile must be a non-empty profile name")
        try:
            target_profile = profile_mod.require(name)
            if target_profile.name not in {p.name for p in profile_mod.list_all()}:
                return json_error(400, "profile must name an existing profile")
            if lineage.effective_harness(target_profile) != harness_registry.CLAUDE_HARNESS:
                return json_error(400, "permission mode is only available for Claude Code profiles")
        except (profile_mod.ProfileError, lineage.LineageError) as exc:
            return json_error(400, str(exc))
    if "mode" not in body:
        return json_error(400, "mode is required; use null to restore the default")
    raw = body.get("mode")
    declared = str(raw).strip() if raw is not None else ""
    if declared and not settings.is_permission_mode(declared):
        return json_error(
            400,
            f"unknown permission mode {declared!r} (known: "
            f"{', '.join(settings.PERMISSION_MODES)})",
        )
    if target_profile is not None:
        store.set_profile_setting(target_profile.name, PERMISSION_MODE_KEY, declared or None)
    elif declared:
        plugins.set_shared_setting(PERMISSION_MODE_KEY, declared)
    else:
        # Undeclaring pops the key. Setting it to ``None`` would be a
        # different act: the store would keep the key and the convergence
        # would write ``"defaultMode": null`` into every profile -- a value
        # Claude Code ignores, so the mode would fall back to asking while
        # the page reported a value nobody chose.
        plugins.unset_shared_setting(PERMISSION_MODE_KEY)
    targets = [target_profile] if target_profile is not None else _converge_profiles()
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
            "profile": target_profile.name if target_profile is not None else None,
            "declared": declared or None,
            "target": (
                store.effective_profile_settings(target_profile.name)
                if target_profile is not None else store.effective_shared_settings()
            ).get(PERMISSION_MODE_KEY),
            "converged": [r.profile for r in results if r.changed],
            "unchanged": [r.profile for r in results if not r.changed and r.ok],
            "failed": failed,
        }
    )


async def h_install_overview(request: web.Request) -> web.Response:
    """Every install command this machine can run, each previewed as a dry run.

    Counts and pending paths per target (see :func:`install_plan.overview`);
    nothing is written. File reads across every profile, so off the loop.
    """
    return json_response(await asyncio.to_thread(install_plan.overview))


async def h_install_plan(request: web.Request) -> web.Response:
    """One target's dry run in full: every file, its before/after text, the
    lines the CLI would print and the effect of each kind of write."""
    target = str(request.query.get("target") or "").strip()
    if not target:
        return json_error(400, "target is required")
    try:
        return json_response(await asyncio.to_thread(install_plan.plan, target))
    except install_plan.TargetError as exc:
        return json_error(404, str(exc))


async def h_install_apply(request: web.Request) -> web.Response:
    """Run one install target for real, from the daemon rather than a shell.

    ``{"target": <id>}``, the ids ``GET /api/install`` lists. The answer
    carries the dry run taken just before (what it was about to change) and
    the lines the run printed -- the same lines the CLI command prints.
    """
    body = await _json_body(request)
    target = str(body.get("target") or "").strip()
    if not target:
        return json_error(400, "target is required")
    try:
        result = await asyncio.to_thread(install_plan.apply, target)
    except install_plan.TargetError as exc:
        return json_error(404, str(exc))
    except (OSError, ProfileError) as exc:
        return json_error(500, f"install failed: {exc}")
    log.info("install target %s applied from the web", target)
    return json_response(result)


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


async def h_metering_live(request: web.Request) -> web.Response:
    """What a session's backend is doing right now (``metering.live_summary``).

    ``?session=<name>`` is required. Answers ``{"session", "live"}`` with
    ``live`` null when the session has no live file (never went through a
    shim of this build). Polled about twice a second by the page while the
    session's terminal is on screen, so it reads only that session's files.
    """
    session = str(request.query.get("session") or "").strip()
    if not session:
        return json_error(400, "session is required")
    live = await asyncio.to_thread(metering.live_summary, session)
    return json_response({"session": session, "live": live})


async def h_usage(request: web.Request) -> web.Response:
    """Return subscription usage for a profile selector.

    Usage providers perform network and subprocess I/O. Run them in a worker
    thread so one slow account endpoint cannot block the event loop. Dashboard
    page-entry reads opt into the profile usage cooldown with ``refresh=auto``;
    an unqualified or ``refresh=manual`` request fetches immediately.
    """
    selector = str(request.query.get("profile") or "").strip()
    if not selector:
        return json_error(400, "profile is required")
    manual = request.query.get("refresh") != "auto"
    try:
        payload, cached, updated_at = await asyncio.to_thread(
            profile_usage.fetch, selector, manual=manual
        )
    except (
        profile_mod.ProfileError,
        usage.UsageError,
        credentials.CredentialsError,
    ) as exc:
        return json_error(400, str(exc))
    return json_response(
        payload,
        headers={
            "X-Usage-Cached": "true" if cached else "false",
            "X-Usage-Updated-At": str(updated_at),
        },
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


async def h_harness_models_save(request: web.Request) -> web.Response:
    """Edit only a harness's model choices; null restores its declaration."""
    name = request.match_info["name"]
    body = await _json_body(request)
    if "models" not in body:
        return json_error(400, "models is required (null restores defaults)")
    try:
        models = (
            None if body["models"] is None
            else harness_registry.validate_model_choices(body["models"])
        )

        def mutate(doc: dict) -> None:
            if harness_registry.get(name, doc) is None:
                raise KeyError(name)
            section = doc.get("model_choices", {})
            if not isinstance(section, dict):
                raise ValueError("model_choices must be a mapping; repair the config first")
            if models is None:
                section.pop(name, None)
            else:
                section[name] = models
            if section:
                doc["model_choices"] = section
            else:
                doc.pop("model_choices", None)

        saved = store.update(mutate)
    except KeyError:
        return json_error(404, f"unknown harness: {name}")
    except (harness_registry.HarnessConfigError, ValueError) as exc:
        return json_error(400, str(exc))
    except store.StoreError as exc:
        return json_error(500, str(exc))
    return json_response({"harness": harness_registry.get(name, saved).to_dict()})


async def h_projects(request: web.Request) -> web.Response:
    """The projects meshes and sessions are filed under, for the pickers.

    Each carries its default workspace both as the registered *name* and as
    the resolved directory (``default_cwd``), so the create form can fill
    its directory field from a project choice without a second round trip.
    """
    return json_response({"projects": [p.to_dict() for p in projects.list_all()]})


async def h_project_add(request: web.Request) -> web.Response:
    """Create a project — the browser's ``claunch project add``.

    Idempotent like the CLI: an existing name with the same default
    workspace is returned as it is, and a different ``default_workspace``
    updates the setting. The workspace must already be registered, because
    a default that names nothing would send every session of the project
    nowhere.
    """
    body = await _json_body(request)
    ws = body.get("default_workspace")
    mesh_args, error = _project_mesh_arg(request, body)
    if error:
        return json_error(400, error)
    try:
        project = projects.add(
            str(body.get("name") or ""),
            default_workspace=None if ws is None else str(ws or ""),
            **mesh_args,
        )
    except projects.ProjectError as exc:
        return json_error(400, str(exc))
    return json_response({"project": project.to_dict()}, status=201)


def _project_mesh_arg(request: web.Request, body: dict) -> Tuple[dict, str]:
    """The ``default_mesh`` keyword a project write passes on, or an error.

    The body says it three ways, one per state the setting has
    (:mod:`claude_launcher.projects`): ``"default_mesh": "<name>"`` sets it,
    ``"default_mesh": null`` sets it to no mesh, and
    ``"default_mesh_unset": true`` drops it back to never-configured. Absent
    both keys, nothing is passed and the setting is left as it is. A name
    must be a mesh this daemon can resolve, for the same reason a default
    workspace must be registered.
    """
    unset = bool(body.get("default_mesh_unset"))
    if unset and "default_mesh" in body:
        return {}, "send either 'default_mesh' or 'default_mesh_unset', not both"
    if unset:
        return {"default_mesh": projects.UNSET}, ""
    if "default_mesh" not in body:
        return {}, ""
    value = body.get("default_mesh")
    if value is None or not str(value).strip():
        return {"default_mesh": None}, ""
    try:
        name = projects.check_mesh_name(value)
        _mesh_mgr(request).get(name)
    except projects.ProjectError as exc:
        return {}, str(exc)
    except MeshError as exc:
        return {}, str(exc)
    return {"default_mesh": name}, ""


async def h_project_update(request: web.Request) -> web.Response:
    """Change a project's defaults: ``default_workspace`` (null/"" clears it)
    and/or the default mesh (see :func:`_project_mesh_arg`)."""
    body = await _json_body(request)
    if not any(k in body for k in ("default_workspace", "default_mesh", "default_mesh_unset")):
        return json_error(
            400,
            "send 'default_workspace' (null clears it), 'default_mesh' "
            "(null = no mesh) or 'default_mesh_unset': true",
        )
    try:
        project = projects.require(request.match_info["name"])
    except projects.ProjectError as exc:
        return json_error(404, str(exc))
    mesh_args, error = _project_mesh_arg(request, body)
    if error:
        return json_error(400, error)
    ws = None
    if "default_workspace" in body:
        ws = str(body.get("default_workspace") or "")
    try:
        project = projects.add(project.name, default_workspace=ws, **mesh_args)
    except projects.ProjectError as exc:
        return json_error(400, str(exc))
    return json_response({"project": project.to_dict()})


async def h_project_remove(request: web.Request) -> web.Response:
    """Drop one project from the registry. Its sessions and meshes keep the
    name on their records and are neither killed nor moved."""
    try:
        removed = projects.remove(request.match_info["name"])
    except projects.ProjectError as exc:
        return json_error(404 if "no project named" in str(exc) else 400, str(exc))
    return json_response({"project": removed.to_dict()})


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


def _worktree_sessions(manager: "SessionManager") -> List[dict]:
    """Every session record reduced to what the worktree join reads."""
    out: List[dict] = []
    for session in manager.list():
        out.append({
            "name": session.sdef.name,
            "cwd": session.sdef.cwd or "",
            "category": session_mod.session_category(session),
            "status": session.status() if not session.exited else "exited",
            "created_at": session.created_at,
        })
    return out


def _worktree_roots(sessions: List[dict]) -> List[str]:
    return worktree_inventory.repo_roots(
        [w.path for w in workspaces.list_all()] + [s["cwd"] for s in sessions]
    )


async def h_worktrees(request: web.Request) -> web.Response:
    """The launcher worktrees of every repository the fleet has touched.

    Each carries its branch, whether that branch is merged into the trunk,
    when it was made, and the sessions whose directory is inside it -- the
    reading the web UI's Worktrees page is drawn from. See
    :mod:`claude_launcher.daemon.worktree_inventory` for the three states.
    """
    manager: SessionManager = request.app["manager"]
    sessions = _worktree_sessions(manager)
    answer = await asyncio.to_thread(
        lambda: worktree_inventory.inventory(_worktree_roots(sessions), sessions)
    )
    return json_response(answer)


async def h_worktree_changes(request: web.Request) -> web.Response:
    """Read changes only for a registered launcher checkout, on expansion."""
    path = request.query.get("path", "")
    if not path:
        return json_response({"error": "path is required"}, status=400)
    sessions = _worktree_sessions(request.app["manager"])

    def read():
        found = worktree_inventory.find(_worktree_roots(sessions), path)
        if found is None:
            return None
        return worktree_inventory.changes(path)

    try:
        files = await asyncio.to_thread(read)
    except worktree_mod.WorktreeError as exc:
        return json_response({"error": str(exc)}, status=500)
    if files is None:
        return json_response({"error": "워크트리를 찾을 수 없습니다"}, status=404)
    return json_response({"path": path, "files": files})


async def h_worktrees_remove(request: web.Request) -> web.Response:
    """Remove launcher worktrees, one or a batch; the branches stay.

    Body: ``{"paths": [...], "archive": false, "force": false}``.

    A checkout is refused while any session joined to it is not archived
    (running, paused or killed): such a session can come back into that
    directory. ``archive: true`` archives those sessions first -- ending the
    running ones, as the session archive route does -- and then removes the
    checkout. ``force`` discards modified and untracked files, which git
    otherwise refuses to delete.

    Partial results are reported rather than raised: one checkout that
    cannot go is no reason to keep the rest of a batch.
    """
    manager: SessionManager = request.app["manager"]
    try:
        body = await request.json()
    except Exception:
        return json_error(400, "body must be JSON")
    paths = body.get("paths") if isinstance(body, dict) else None
    if not isinstance(paths, list) or not paths or not all(
        isinstance(p, str) and p for p in paths
    ):
        return json_error(400, "'paths' must be a non-empty list of paths")
    archive = bool(body.get("archive"))
    force = bool(body.get("force"))
    removed: List[dict] = []
    failed: List[dict] = []
    archived: List[str] = []
    sessions = _worktree_sessions(manager)
    known = await asyncio.to_thread(
        lambda: worktree_inventory.index(_worktree_roots(sessions))
    )
    for path in paths:
        found = known.get(worktree_inventory.path_key(path))
        if found is None:
            failed.append({"path": path, "error": "not a launcher worktree"})
            continue
        root, _ = found
        live = [
            s for s in _worktree_sessions(manager)
            if s["category"] != session_mod.CATEGORY_ARCHIVED
            and s["cwd"] and worktree_inventory.inside(s["cwd"], path)
        ]
        if live and not archive:
            failed.append({
                "path": path,
                "error": "sessions that are not archived use this worktree",
                "sessions": [s["name"] for s in live],
            })
            continue
        name = ""
        try:
            for s in live:
                name = s["name"]
                session = manager.get(name)
                if session.exited:
                    manager.archive(name)
                else:
                    request.app["handoff"].forget(name)
                    request.app["beads"].winddowns.pop(name, None)
                    await manager.stop_and_archive(
                        name, by=actors.with_reason(
                            actors.from_request(request), "worktree-remove"))
                # Recorded per session as it lands, so a failure further
                # down the list still reports the ones already archived.
                archived.append(name)
        except Exception as exc:
            failed.append({
                "path": path, "error": f"archive of {name} failed: {exc}",
                "sessions": [name],
            })
            continue
        try:
            done = await asyncio.to_thread(
                worktree_inventory.remove, root, path, force=force
            )
        except (worktree_inventory.RemoveError, worktree_mod.WorktreeError) as exc:
            failed.append({"path": path, "error": str(exc)})
            continue
        removed.append(done)
    return json_response(
        {"removed": removed, "failed": failed, "archived": archived}
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


def _prompt_packs_doc() -> dict:
    """Everything the Settings ▸ Install card draws, in one read."""
    roleset = mesh_roles.resolve()
    return {
        "catalog": [p.to_dict() for p in prompt_packs.catalog()],
        "workspaces": [
            {**w.to_dict(), "packs": prompt_packs.workspace_rows(Path(w.path))}
            for w in workspaces.list_all()
        ],
        "role_packs": prompt_packs.role_packs(),
        "default_role_packs": prompt_packs.DEFAULT_ROLE_PACKS,
        "default_packs": prompt_packs.default_packs(),
        "shipped_default_packs": prompt_packs.DEFAULT_PACKS,
        # Shown for install like any pack, kept out of the two settings.
        "on_demand_packs": prompt_packs.on_demand_ids(),
        "roles": sorted(roleset.roles),
    }


async def h_prompt_packs(request: web.Request) -> web.Response:
    """Shipped packs, what each workspace holds, and the two settings."""
    return json_response(await asyncio.to_thread(_prompt_packs_doc))


async def h_prompt_packs_offer(request: web.Request) -> web.Response:
    """What the create form offers for one directory.

    The packs installed in the workspace that owns ``cwd`` (none when no
    workspace does), which of them start ticked, and the role → pack mapping
    so the form can lock the ones a chosen role or subrole requires.
    ``shipped`` is the package catalog: the form offers those the workspace
    has not installed as boxes too, and one that is ticked or required goes in
    as the shipped copy (prompt_packs.pack_copy), named as such.
    An on-demand pack (prompt_packs.ON_DEMAND) is in neither list: it is
    never part of an opening, so the form has nothing to offer for it.
    """
    cwd = cflow_state.resolve_cwd(request.query.get("cwd") or None)
    owner = workspaces.owning(str(cwd))
    root = Path(owner.path) if owner is not None else None
    packs = (
        [p.to_dict() for p in prompt_packs.installed(root)
         if not prompt_packs.is_on_demand(p.id, root)]
        if root is not None else []
    )
    return json_response({
        "cwd": str(cwd),
        "workspace": owner.to_dict() if owner is not None else None,
        "packs": packs,
        # Ticked on every opening of the form, installed or not.
        "default_packs": prompt_packs.default_packs(),
        "role_packs": prompt_packs.role_packs(),
        "shipped": [p.to_dict() for p in prompt_packs.catalog()
                    if not p.on_demand],
        # This daemon's own prompt, null when it has none: the form draws it
        # first and ticked, and sends daemon_prompt: false when unticked.
        "daemon_prompt": daemon_prompt.offer(),
    })


async def h_prompt_packs_text(request: web.Request) -> web.Response:
    """One pack's text as a session created in ``cwd`` would be given it.

    The create form's preview: the workspace's copy when installed (edits
    included), else claunch's, with where it came from
    (prompt_packs.preview). 404 for an id that is neither.
    """
    pack_id = request.query.get("id") or ""
    cwd = cflow_state.resolve_cwd(request.query.get("cwd") or None)
    owner = workspaces.owning(str(cwd))
    try:
        doc = await asyncio.to_thread(
            prompt_packs.preview,
            Path(owner.path) if owner is not None else None,
            pack_id,
        )
    except prompt_packs.PackError as exc:
        return json_error(404, str(exc))
    doc["workspace"] = owner.to_dict() if owner is not None else None
    return json_response(doc)


async def h_daemon_prompt_get(request: web.Request) -> web.Response:
    """This daemon's own prompt: where it lives, what it says."""
    return json_response(await asyncio.to_thread(daemon_prompt.read))


async def h_daemon_prompt_put(request: web.Request) -> web.Response:
    """Replace this daemon's prompt (``text``; empty turns it off).

    ``by`` names the writer for the log beside the file -- the MCP tool sends
    the calling session, Settings sends nothing and is logged as the web UI.
    """
    body = await _json_body(request)
    by = str(body.get("by") or "web")
    try:
        doc = await asyncio.to_thread(
            daemon_prompt.write, body.get("text", ""), by=by
        )
    except daemon_prompt.DaemonPromptError as exc:
        return json_error(400, str(exc))
    return json_response(doc)


def _pack_target(body: dict):
    ws = workspaces.find(str(body.get("workspace") or ""))
    if ws is None:
        raise prompt_packs.PackError(
            f"no registered workspace {body.get('workspace')!r}"
        )
    return Path(ws.path), str(body.get("pack") or "")


async def h_prompt_packs_install(request: web.Request) -> web.Response:
    """Copy a shipped pack into a workspace (``force`` over an edited copy)."""
    body = await _json_body(request)
    try:
        root, pack_id = _pack_target(body)
        before = prompt_packs.install(root, pack_id, force=body.get("force") is True)
    except prompt_packs.PackError as exc:
        return json_error(400, str(exc))
    return json_response({"pack": pack_id, "was": before, **_prompt_packs_doc()})


async def h_prompt_packs_install_all(request: web.Request) -> web.Response:
    """Install all shipped packs in one registered workspace, keeping edits."""
    body = await _json_body(request)
    try:
        root, _ = _pack_target(body)
    except prompt_packs.PackError as exc:
        return json_error(400, str(exc))
    results = await asyncio.to_thread(prompt_packs.install_all, root)
    return json_response({"batch": results, **await asyncio.to_thread(_prompt_packs_doc)})


async def h_prompt_packs_remove(request: web.Request) -> web.Response:
    """Delete a workspace's copy (``force`` for an edited or local one)."""
    body = await _json_body(request)
    try:
        root, pack_id = _pack_target(body)
        before = prompt_packs.remove(root, pack_id, force=body.get("force") is True)
    except prompt_packs.PackError as exc:
        return json_error(400, str(exc))
    return json_response({"pack": pack_id, "was": before, **_prompt_packs_doc()})


async def h_prompt_packs_role_packs(request: web.Request) -> web.Response:
    """Save the role → required packs mapping and/or the default packs.

    Each key is optional; ``null`` restores that setting's default.
    """
    body = await _json_body(request)
    if "role_packs" not in body and "default_packs" not in body:
        return json_error(400, "send role_packs and/or default_packs")
    try:
        # Both checked before either is written, so a bad half saves nothing.
        if body.get("role_packs") is not None:
            prompt_packs.clean_role_packs(body["role_packs"])
        if isinstance(body.get("default_packs"), list):
            prompt_packs.check_offerable(
                [prompt_packs.check_id(str(i)) for i in body["default_packs"]],
                "a default pack",
            )
        if "role_packs" in body:
            prompt_packs.set_role_packs(body["role_packs"])
        if "default_packs" in body:
            prompt_packs.set_default_packs(body["default_packs"])
    except prompt_packs.PackError as exc:
        return json_error(400, str(exc))
    return json_response(_prompt_packs_doc())


#: The ``llm`` block fields the Settings page may write. ``params`` is left
#: out on purpose: it is a free map of extra chat/completions fields, which a
#: text box cannot validate and which nothing in the UI reads back.
LLM_EDITABLE = frozenset({"profile", "model", "endpoint", "api_key", "max_tokens"})
#: Upper bound for the completion budget a form may save. The endpoint's own
#: limit is the real one; this only keeps a typo (a pasted model id, a stray
#: zero) from being written as a number the call then spends a minute failing
#: on.
LLM_MAX_TOKENS_CEILING = 1_000_000


def _llm_profile_rows(doc: dict) -> List[dict]:
    """Every on-disk profile the briefing could call, with what it resolves to.

    Profiles whose provider names no OpenAI-compatible endpoint are left out
    rather than offered and refused on save: the reason belongs to the
    provider, and this list is a picker, not a diagnosis. The one profile
    that *is* configured is reported separately (``error`` below), so a
    hand-edited name that cannot serve still says why.
    """
    rows: List[dict] = []
    for prof in profile_mod.list_all():
        try:
            backend = briefing.profile_backend(prof.name, doc)
        except briefing.BriefingError:
            continue
        rows.append(
            {
                "name": prof.name,
                "endpoint": backend.endpoint,
                "models": list(backend.models),
                "has_key": bool(backend.api_key),
            }
        )
    return rows


def _llm_settings_payload(doc: Optional[dict] = None) -> dict:
    """What the Settings card draws, and what a save answers with.

    Two layers, deliberately both: ``profile``/``model``/``endpoint`` are the
    block as *written* (what the form's fields hold), ``resolved`` is what a
    call would actually use after a profile is applied. A card showing only
    the second could not tell a typed endpoint from an inherited one; one
    showing only the first could not show that a profile supplies the key.

    No secret crosses this boundary. The key itself is never returned —
    ``api_key_set`` says the block holds one, ``resolved.has_key`` says a
    call would have one (from the block or from the profile).
    """
    doc = store.load() if doc is None else doc
    block = doc.get("llm")
    if not isinstance(block, dict):
        block = {}
    cfg = briefing.llm_config(doc)
    error = ""
    if cfg["profile"]:
        try:
            briefing.profile_backend(cfg["profile"], doc)
        except briefing.BriefingError as exc:
            error = str(exc)
    return {
        "profile": cfg["profile"],
        "model": str(block.get("model") or "").strip(),
        "endpoint": str(block.get("endpoint") or "").strip(),
        "max_tokens": cfg["max_tokens"],
        "api_key_set": bool(str(block.get("api_key") or "").strip()),
        "configured": briefing.llm_configured(cfg),
        "resolved": {
            "endpoint": cfg["endpoint"],
            "model": cfg["model"],
            "has_key": bool(cfg["api_key"]),
        },
        "error": error,
        "profiles": _llm_profile_rows(doc),
    }


def _llm_block_update(body: dict, doc: dict) -> dict:
    """Validate a save and return the ``llm`` block it would write.

    ``api_key`` follows the convention the search settings set: an empty
    string keeps the stored secret (a password field arrives blank on every
    reload), and ``null`` is the explicit "remove it" — without that, a key
    typed for a backend nobody calls any more would sit in the file with no
    way to take it out from the page that put it there. Saving a profile is
    one such moment, and the card says so itself: it sends ``""`` for the
    endpoint and ``null`` for the key, because the profile now supplies both
    and the pair left behind describes whatever was typed in before.
    """
    if not isinstance(body, dict) or set(body) - LLM_EDITABLE:
        raise ValueError("unsupported briefing llm setting")
    block = dict(doc.get("llm") if isinstance(doc.get("llm"), dict) else {})
    for key, value in body.items():
        if key == "max_tokens":
            if isinstance(value, bool) or not isinstance(value, int):
                raise ValueError("max_tokens must be a whole number")
            if value < 1 or value > LLM_MAX_TOKENS_CEILING:
                raise ValueError(
                    f"max_tokens must be between 1 and {LLM_MAX_TOKENS_CEILING}"
                )
            block["max_tokens"] = value
            continue
        if key == "api_key" and value is None:
            block.pop("api_key", None)
            continue
        if not isinstance(value, str):
            raise ValueError(f"{key} must be text")
        value = value.strip()
        if key == "api_key":
            if not value:
                continue  # a blank password field preserves the saved secret
            block["api_key"] = value
            continue
        if key == "profile" and value:
            # A name the picker did not offer is still checked, because this
            # endpoint is also how a script saves: an unknown profile would
            # otherwise turn the feature off with nothing said.
            try:
                profile_mod.require_selector(value)
            except ProfileError as exc:
                raise ValueError(str(exc)) from exc
        if key == "endpoint" and value:
            address = urlsplit(value)
            if (
                address.scheme not in ("http", "https")
                or not address.hostname
                or address.username
                or address.password
                or address.query
                or address.fragment
            ):
                raise ValueError(
                    "endpoint must be an HTTP(S) URL without credentials, "
                    "query or fragment"
                )
        if value:
            block[key] = value
        else:
            block.pop(key, None)
    return block


async def h_briefing_llm(request: web.Request) -> web.Response:
    """Which backend and model the session briefing calls."""
    return json_response(_llm_settings_payload())


async def h_briefing_llm_save(request: web.Request) -> web.Response:
    """Write the ``llm`` block from the Settings card.

    Cached briefings are dropped when the resolved backend changed, so the
    next card is composed by the model that was just chosen instead of
    showing the previous one's answer until the session's transcript moves.
    """
    body = await _json_body(request)
    doc = store.load()
    before = briefing.llm_config(doc)
    try:
        block = _llm_block_update(body, doc)
    except ValueError as exc:
        return json_error(400, str(exc))
    try:
        def mutate(document: dict) -> None:
            if block:
                document["llm"] = block
            else:
                document.pop("llm", None)

        saved = store.update(mutate)
    except store.StoreError as exc:
        return json_error(500, str(exc))
    after = briefing.llm_config(saved)
    if (after["endpoint"], after["model"]) != (before["endpoint"], before["model"]):
        briefing.forget_all()
    return json_response(_llm_settings_payload(saved))


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


async def h_prompter_get(request: web.Request) -> web.Response:
    """The prompter server setting: ``{"url": <base URL or null>}``."""
    try:
        return json_response({"url": prompter.configured_url()})
    except store.StoreError as exc:
        return json_error(500, str(exc))


async def h_prompter_set(request: web.Request) -> web.Response:
    """Set or clear (blank/null ``url``) the prompter server's base URL.

    Answers with the stored URL and a fresh fetch of its prompts, so the
    settings page can say at once whether the address works.
    """
    body = await _json_body(request)
    try:
        url = prompter.set_url(body.get("url"))
        answer = await prompter.prompts(refresh=True)
        return json_response({"url": url, "status": answer})
    except prompter.PrompterURLError as exc:
        return json_error(400, str(exc))
    except store.StoreError as exc:
        return json_error(500, str(exc))


def _wsl_reg_stub_state() -> dict:
    return {
        "enabled": wsl_reg_stub.enabled(store.daemon_config()),
        "wsl": wsl_reg_stub.is_wsl(),
    }


async def h_wsl_reg_stub_get(request: web.Request) -> web.Response:
    """``daemon.wsl_hide_reg_exe`` and whether this daemon runs under WSL."""
    try:
        return json_response(_wsl_reg_stub_state())
    except store.StoreError as exc:
        return json_error(500, str(exc))


async def h_wsl_reg_stub_set(request: web.Request) -> web.Response:
    """Turn ``daemon.wsl_hide_reg_exe`` on or off (``{"enabled": bool}``).

    Applies to sessions spawned from now on; running ones are left as they
    started."""
    body = await _json_body(request)
    enabled = body.get("enabled")
    if not isinstance(enabled, bool):
        return json_error(400, "'enabled' must be true or false")
    try:
        store.set_daemon_field(wsl_reg_stub.SETTING, enabled)
        return json_response(_wsl_reg_stub_state())
    except store.StoreError as exc:
        return json_error(500, str(exc))


async def h_prompter_prompts(request: web.Request) -> web.Response:
    """The prompter server's prompt snippets for the footer.

    Always 200: an unset URL answers ``enabled: false`` and an unreachable
    server ``connected: false`` — both with an empty list the UI hides.
    ``?refresh=1`` skips the short cache.
    """
    refresh = request.query.get("refresh") in ("1", "true", "yes")
    try:
        return json_response(await prompter.prompts(refresh=refresh))
    except store.StoreError as exc:
        return json_error(500, str(exc))


async def h_prompter_contexts(request: web.Request) -> web.Response:
    """Context snippets for the New/Spawn prompt-pack selection."""
    refresh = request.query.get("refresh") in ("1", "true", "yes")
    try:
        return json_response(await prompter.contexts(refresh=refresh))
    except store.StoreError as exc:
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
            # What a blank role resolves to when nothing in the handle names
            # one -- the create form needs it to say which role packs follow.
            "default": roleset.default,
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

#: How much of a finished run's journal the session detail panel is sent.
#: The panel draws the newest 40 lines and the total (app.js
#: SESS_RUN_JOURNAL), out of the step name and the event -- never the
#: summary or the details, which reach it as ``reports``. Whole, the journal
#: of a 63-step run was 135KB of a 166KB answer, polled every five seconds
#: (s586 on 2026-09-20).
_META_JOURNAL_TAIL = 40

#: Fields of a journal entry the detail panel does not draw. They are the
#: run's own record, which the agent-facing payload still carries in full.
_META_JOURNAL_DROP = ("summary", "details")


def _clip_journal(entry: dict) -> dict:
    """A run entry with its journal cut to what the detail panel draws.

    Only a finished run has one at all (``engine._done_payload``), which is
    why this is worth its own function rather than a line: the size arrives
    with the last step, so a run that was cheap to poll all round becomes
    expensive exactly when it ends.
    """
    journal = entry.get("journal")
    if not isinstance(journal, list):
        return entry
    return {
        **entry,
        "journal": [
            {k: v for k, v in e.items() if k not in _META_JOURNAL_DROP}
            for e in journal[-_META_JOURNAL_TAIL:]
        ],
        # The count is over the whole journal, so "newest 40 of N" stays
        # true after the cut.
        "journal_total": len(journal),
    }


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


def _operator_gate_kind(entry: dict) -> str:
    """Which press settles this run position, or '' when no person's does.

    The same reading as the dashboard's rail (app.js ``sessCflowGated``): an
    approval, a user-chosen branch, a goto request, or an agent question that
    reached nobody. A timer, a checklist, a window and a question still with
    another agent wait on no one here, so the operator does not raise them.
    """
    status = str(entry.get("status") or "")
    if status == "waiting_approval":
        return "approval"
    if status == "waiting_selection":
        return "selection"
    if status == "waiting_goto":
        return "goto"
    if status == "waiting_answer" and not ((entry.get("ask") or {}).get("asked") or []):
        return "selection" if _operator_gate_options(entry) else "approval"
    return ""


def _operator_gate_options(entry: dict) -> list:
    ask = entry.get("ask") or {}
    raw = ask.get("options") if ask.get("kind") == "branch" else entry.get("options")
    return [{"name": str(o.get("name")), "description": o.get("description") or ""}
            for o in raw or [] if isinstance(o, dict) and o.get("name")]


def _operator_gates(sessions) -> list:
    """The cflow gates waiting on a person among ``sessions`` — what an
    operator surfaces to its user, and what its panel offers to press. Read
    off the event loop by the caller.

    Only the live sessions' own runs, one slot each, keyed the same way the
    rail binds a run to a session. Each carries the ``cwd`` and ``scope`` the
    dashboard's own presses post (``/api/cflow/approve``, ``select``,
    ``goto/resolve``), so the Operator panel settles it the same way.
    """
    gates = []
    for session in sessions:
        cwd = _session_cwd(session)
        if session.exited or not cwd:
            continue
        name = session.sdef.name
        try:
            entry = _slim_cflow_payload(cflow_engine.status(cwd, scope=name))
        except (CflowError, WorkflowError, StateError, OSError):
            continue
        kind = _operator_gate_kind(entry)
        if not kind:
            continue
        ask = entry.get("ask") or {}
        goto = entry.get("goto_request") or {}
        gates.append({"session": name, "cwd": cwd, "scope": name, "kind": kind,
                      "status": entry.get("status"), "run": entry.get("run"),
                      "workflow": entry.get("workflow"), "step_id": entry.get("step_id"),
                      "title": entry.get("title"), "reason": entry.get("reason"),
                      "prompt": _clip(ask.get("prompt") or entry.get("prompt") or entry.get("gate"), 1200),
                      "options": _operator_gate_options(entry) if kind == "selection" else [],
                      "goto_request": ({"step": goto.get("step"), "from": goto.get("from"),
                                        "reason": _clip(goto.get("reason"), 1200)} if goto else None),
                      "ask": entry.get("ask")})
    return gates


async def _operator_work(app, sessions) -> dict:
    """Each session's work progress, for the operator: the status-check
    answers it last reported (the user's commit / tests / merge questions) and
    the board issue it is on — the first open one the board matches to it
    (the linked one first, in the rail's own order), else the most relevant
    closed one: a session that closed its opening issue and took the next
    keeps the old link, so the link alone would report a finished round. The
    board read is the rail's own (cached, one listing per board), so an
    operator poll costs no more than a rail refresh."""
    board = app.get("beads")
    out = {}
    for session in sessions:
        name = session.sdef.name
        checks = session.info().get("status_checks") or []
        out[name] = {"checks": [c for c in checks if isinstance(c, dict)], "issue": None}
        if board is None or not board.available() or not session.sdef.cwd:
            continue
        try:
            root = await board.root_for(session.sdef.cwd)
            if not board.has_board(root):
                continue
            rows = await board.issues(root)
        except Exception:  # an unreadable board leaves the issue unknown
            continue
        found = beads_mod.match(rows, name, issue=session.sdef.issue, task=session.sdef.task)
        active = [m for m in found if m.get("status") != "closed"]
        pick = (active or found or [None])[0]
        if pick:
            out[name]["issue"] = {"id": pick.get("id"), "status": pick.get("status"),
                                  "title": pick.get("title")}
    return out


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
    include_held: bool = False,
) -> list:
    """The session this run maps 1:1 to (scope == session name; a v2 slot of
    this daemon without its ``@<daemon>``), if alive.

    ``include_held`` also answers a held record (daemon/hold.py): a person
    acting on its run is a use of it, and the nudge that follows wakes it
    (:func:`_cflow_nudge_held`, claunch-2xyjw)."""
    name = cflow_clock.session_name(scope)
    if not name:
        return []
    if live_sessions is not None:
        return [name] if (cwd, name) in live_sessions else []
    for session in manager.list():
        if (
            session.sdef.name == name
            and (not session.exited or (include_held and cflow_clock.is_held(session)))
            and _session_cwd(session) == cwd
        ):
            return [name]
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

    A run nobody is driving carries ``orphaned: true``
    (:func:`cflow_clock.run_orphaned`), which the dashboard's Orphans tab
    lists. That tab is the standing view of the set; the run event clock
    pushes only a crash, once, at the exit.
    """
    manager: SessionManager = request.app["manager"]
    # Resolve the session registry once for the whole sweep.  The old path
    # called ``manager.list()`` once per run to bind a scope, then ``get()``
    # once more per run for its timers.  A busy machine has hundreds of run
    # slots, so that repeated sorting and delayed-Codex discovery dominated
    # the endpoint even when every run file was cached.
    # Every registered session can have a rail card, including held, killed,
    # paused and archived records. Bind runs by directory and name regardless
    # of process state; retain the existing live/paused timer lookup separately.
    def _live_or_paused(session) -> bool:
        if not session.exited:
            return True
        return bool(getattr(session, "paused_at", None)) and not getattr(
            session, "archived_at", None
        )

    rail_sessions = {
        (_session_cwd(session), session.sdef.name): session
        for session in manager.list()
        if _session_cwd(session)
    }
    live_sessions = {
        key: session for key, session in rail_sessions.items()
        if _live_or_paused(session)
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
            if not _valid_cflow2_scope(wanted):
                return json_error(400, f"invalid scope: {wanted!r}")
            # A v2 slot ('s15@main'): it has no v1 run to list.
            scopes, wanted = [], None
        for scope in [wanted] if wanted else scopes:
            if (explicit, scope) not in keys:
                keys.append((explicit, scope))

    # The background poll annotates all registered session rows. Historical
    # unbound runs remain available from the Flows page through the default
    # response, but they no longer require state/journal reads or network
    # transfer in every tab every two seconds.
    if rail_view and not explicit:
        keys = [key for key in keys if key in rail_sessions]

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
                live_sessions=rail_sessions if rail_view else live_sessions,
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
                # Carried on the entry so the Flows page can list the runs
                # nobody is driving without re-deriving the rule. Skipped
                # on the rail poll, which does not render the Orphans tab.
                if not rail_view and cflow_clock.run_orphaned(
                    manager, cwd, scope, entry
                ):
                    entry["orphaned"] = True
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
                        {"id": i.id, "describe": i.describe, "check": i.check,
                         **({"by": list(i.by)} if i.manual else {})}
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
        if not _valid_cflow2_scope(scope):
            return json_error(400, f"invalid scope: {scope!r}")
        # A v2 slot name ('s15@main'): no v1 run can live there, so this is
        # the idle answer below, pointing the view at /api/cflow2/run.
        return json_response({
            "cwd": cwd, "scope": scope, "status": "idle",
            "sessions": _scope_sessions(request.app["manager"], cwd, scope),
            "pending_start": None,
            **({"v2": True} if cflow2_web.has_run(cwd, scope) else {}),
        })
    manager: SessionManager = request.app["manager"]
    sessions = _scope_sessions(manager, cwd, scope)
    payload = cflow_engine.status(cwd, scope=scope)
    if payload.get("status") == "idle":
        idle = {
            "cwd": cwd,
            "scope": scope,
            "status": "idle",
            "sessions": sessions,
            "pending_start": payload.get("pending_start"),
        }
        # No v1 run, but a v2 one: said here so a view that asked the v1
        # endpoint can turn to /api/cflow2/run instead of reading "idle".
        if cflow2_web.has_run(cwd, scope):
            idle["v2"] = True
        return json_response(idle)
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
    # the run itself). The v2 handlers share this, and a v2 slot is named
    # '<session>@<daemon>' ('+side', '~delegation'), which the v1 check
    # refuses: cflow2's own check is the one that knows those names.
    scope = str(body.get("scope") or "") or cflow_state.DEFAULT_SCOPE
    if not (cflow_state.valid_scope(scope) or _valid_cflow2_scope(scope)):
        return None, json_error(400, f"invalid scope: {scope!r}")
    return (str(cwd), scope, body), None


def _valid_cflow2_scope(scope: str) -> bool:
    try:
        cflow2_store.normalize_scope(scope)
    except cflow2_store.StoreError:
        return False
    return True


async def _nudge_sessions(
    manager: SessionManager, cwd: str, scope: str, message: str
) -> list:
    """Queue a resume nudge for the run's own session (scope == session
    name, 1:1), so an agent that stopped its turn picks the run back up.
    Default-scope runs belong to no session — nothing to nudge."""
    nudged = []
    for name in _scope_sessions(manager, cwd, scope, include_held=True):
        try:
            session = manager.get(name)
        except Exception:  # noqa: BLE001 — raced with a removal
            continue
        if cflow_clock.is_held(session):
            if _cflow_nudge_held(manager, name, message):
                nudged.append(name)
        elif session.queue_delivery(message):
            nudged.append(name)
    return nudged


def _cflow_nudge_held(manager: SessionManager, name: str, message: str) -> bool:
    """Wake a held driver for a person's action on its run and queue the
    nudge into the woken session (claunch-2xyjw). Without this the action
    lands on disk and the run waits until something else wakes it."""
    try:
        return bool(manager.wake_soon(name, by="cflow", deliver=message))
    except Exception:  # noqa: BLE001 — a failed wake leaves it held
        log.exception("wake of held session %r for a cflow nudge failed", name)
        return False


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
    for name in _scope_sessions(manager, cwd, scope, include_held=True):
        try:
            session = manager.get(name)
        except Exception:  # noqa: BLE001 — raced with a removal
            continue
        if cflow_clock.is_held(session):
            # A woken session has no draft to keep whole: ``force`` has
            # nothing to do there.
            if _cflow_nudge_held(manager, name, message):
                scheduled.append(name)
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


#: Composed workflow lists, cwd -> (stamp, entries). See
#: :func:`_startable_workflows` for what the stamp covers.
_WORKFLOW_LISTS: Dict[str, Tuple[tuple, list]] = {}

#: Files a cwd's answer was composed from last time, so a base that is edited
#: without any of the *found* files changing still invalidates the entry.
_WORKFLOW_BASES: Dict[str, Set[str]] = {}


def _file_stamp(paths) -> tuple:
    """Each path with its mtime and size — absent files stamped as absent,
    so a file appearing or going away is a change like any other."""
    out = []
    for path in sorted({str(p) for p in paths}):
        try:
            st = os.stat(path)
        except OSError:
            out.append((path, None, None))
        else:
            out.append((path, st.st_mtime_ns, st.st_size))
    return tuple(out)


def _startable_workflows(cwd: str) -> list:
    """What can be started here, each entry saying which file it would run.

    ``origin``/``shadowed`` travel with the path because the dashboard is
    where somebody picks a workflow by name — the one surface where two
    same-named files in two layers look like one thing.

    Cached against the files it was composed from. Resolving which files
    those are costs under a millisecond; composing them costs 161ms for the
    sixteen declared in this repository, and the session detail panel asks
    for this every five seconds (measured 2026-09-20). That composition is
    synchronous Python, so it is not merely slow for its own caller -- it
    holds the event loop, and every terminal the daemon is pumping stops for
    the length of it. A workflow file changes when somebody edits one; the
    poll is not what should be paying to find that out.
    """
    located = list(cflow_state.resolved_workflows(cwd))
    watched = [f.path for f in located]
    for f in located:
        watched.extend(f.shadows)
    watched.extend(_WORKFLOW_BASES.get(cwd) or ())
    stamp = _file_stamp(watched)
    hit = _WORKFLOW_LISTS.get(cwd)
    if hit is not None and hit[0] == stamp:
        return hit[1]

    bases: Set[str] = set()
    flows = []
    for found in located:
        entry = {
            "name": found.name,
            "path": str(found.path),
            "origin": found.origin,
            "shadowed": [str(p) for p in found.shadows],
        }
        try:
            composed = cflow_state.compose_located(found, cwd)
            bases.update(str(b) for b in composed.bases)
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
        # v1 beside v2 (docs/cflow-v2.md section 10): the format is
        # deprecated, the workflows keep running.
        cflow2_web.v1_marks(entry)
        flows.append(entry)
    # Stamped again with the bases this composition turned out to read, so
    # the next call watches them too. A base found now that was not watched
    # a moment ago makes this entry a miss once, and a hit after that.
    _WORKFLOW_BASES[cwd] = bases
    _WORKFLOW_LISTS[cwd] = (_file_stamp(watched + sorted(bases)), flows)
    return flows


async def h_cflow_workflows(request: web.Request) -> web.Response:
    """Workflows startable in a directory (project + global) — feeds the
    dashboard's start picker."""
    # An absent cwd means the daemon's own directory, which is exactly what a
    # session created with no directory runs in — so the create form's
    # "(daemon cwd)" asks about the workflows it would really see.
    raw = request.query.get("cwd")
    cwd = cflow_state.resolve_cwd(raw)
    return json_response({
        "workflows": await asyncio.to_thread(_startable_all, cwd),
        # Settings' per-role auto-pick overrides, served with the list they
        # pick from so the form and the CLI wizard apply them in one read.
        "role_workflows": _role_workflows_or_none(),
    })


def _role_workflows_or_none() -> dict:
    """The overrides, or none when the config file cannot be read: the list
    is what the picker needs, and an unreadable file must not take it away."""
    try:
        return store.role_workflows()
    except store.StoreError as exc:
        log.warning("could not read daemon.role_workflows: %s", exc)
        return {}


def _startable_all(cwd: str) -> list:
    """v1 and v2 workflows startable in ``cwd``, one list, told apart by
    ``version`` (docs/cflow-v2.md section 9). v1 first, so a picker that
    takes the first match by name keeps v1 on a name both formats declare --
    the same rule the start path applies (:func:`cflow2_web.choose`)."""
    flows = list(_startable_workflows(cwd))
    try:
        flows.extend(cflow2_web.startable(cwd))
    except Exception as exc:  # noqa: BLE001 - a broken v2 layer must not hide v1
        log.warning("could not list cflow v2 workflows in %s: %s", cwd, exc)
    return flows


async def h_cflow_definitions(request: web.Request) -> web.Response:
    """Source declarations for the editor, including shadowed layers."""
    cwd = cflow_state.resolve_cwd(request.query.get("cwd"))
    try:
        # Compiles every v2 file (cached by layer stamp): off the event loop.
        v2 = await asyncio.to_thread(cflow2_web.definitions, cwd)
    except Exception as exc:  # noqa: BLE001 - a broken v2 layer must not hide v1
        log.warning("could not list cflow v2 definitions in %s: %s", cwd, exc)
        v2 = []
    return json_response({
        "definitions": cflow_editor.declarations(cwd), "cwd": cwd,
        # Read-only in the editor (GET /api/cflow2/definition); the v1 rows
        # above are the deprecated format, still editable.
        "definitions_v2": v2,
        "deprecated_note": cflow2_web.DEPRECATED_NOTE,
    })


async def h_cflow_definition(request: web.Request) -> web.Response:
    cwd = cflow_state.resolve_cwd(request.query.get("cwd"))
    try:
        return json_response(cflow_editor.read(
            cwd, request.query.get("name", ""), request.query.get("layer", ""),
            request.query.get("path", "")))
    except (WorkflowError, OSError) as exc:
        return json_error(404, str(exc))


async def _cflow_definition_body(request: web.Request):
    try:
        body = await request.json()
    except (ValueError, web.HTTPError):
        return None, json_error(400, "invalid JSON body")
    if not isinstance(body, dict) or not all(isinstance(body.get(k), str)
                                              for k in ("name", "layer", "text")):
        return None, json_error(400, "name, layer and text strings are required")
    return body, None


async def h_cflow_definition_validate(request: web.Request) -> web.Response:
    body, err = await _cflow_definition_body(request)
    if err:
        return err
    cwd = cflow_state.resolve_cwd(body.get("cwd"))
    try:
        return json_response({"valid": True, "validation": cflow_editor.validate(
            cwd, body["name"], body["layer"], body["text"], body.get("path", ""))})
    except (WorkflowError, OSError) as exc:
        return json_response({"valid": False, "error": str(exc)}, status=400)


async def h_cflow_definition_save(request: web.Request) -> web.Response:
    body, err = await _cflow_definition_body(request)
    if err:
        return err
    if not isinstance(body.get("revision"), str):
        return json_error(400, "revision is required")
    cwd = cflow_state.resolve_cwd(body.get("cwd"))
    try:
        return json_response(cflow_editor.save(
            cwd, body["name"], body["layer"], body["text"], body["revision"],
            body.get("path", "")))
    except cflow_editor.EditConflict as exc:
        return json_error(409, str(exc))
    except (WorkflowError, OSError) as exc:
        return json_error(400, str(exc))


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
    # cflow v2 has no start-request file for the agent to pick up, so a v2
    # workflow asked for here is started directly and the session nudged --
    # the same thing /api/cflow/start does for it.
    v2 = await _cflow2_start(request, cwd, scope, body, context)
    if v2 is not None:
        return v2
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
    v2 = await _cflow2_start(request, cwd, scope, body, context)
    if v2 is not None:
        return v2
    payload = cflow_engine.start(workflow, context=context, cwd=cwd, scope=scope)
    payload["nudge_scheduled_sessions"] = _schedule_cflow_nudges(
        request.app, cwd, scope, cflow_engine.NUDGE_STARTED, force=True
    )
    return json_response(payload)


#: Typed into the scope's session after the dashboard starts a v2 run there.
NUDGE_CFLOW2_STARTED = (
    "cflow2: a new v2 workflow run was started for this session - call the "
    "flow_status tool and drive it with flow_report / flow_select"
)
NUDGE_CFLOW2_ANSWERED = (
    "cflow2: the user answered your run's open question from the dashboard - "
    "call the flow_status tool and continue"
)
#: A choice the user took from the dashboard: which option, and the note they
#: typed with it -- their instruction to the agent (claunch-e437w).
NUDGE_CFLOW2_PICKED = (
    "cflow2: the user picked '{option}' for {label} from the dashboard{note} - "
    "call the flow_status tool and continue (its person_picks keeps the pick "
    "and the whole note)"
)
#: The run page's Nudge button on a v2 run (the v1 line names /cflow).
NUDGE_CFLOW2_CONTINUE = (
    "cflow2: continue your v2 workflow run - call the flow_status tool and drive "
    "it with flow_report / flow_select"
)
NUDGE_CFLOW2_FORCED = (
    "cflow2: the user moved your run from the dashboard - what you were doing "
    "there is cancelled; call the flow_status tool and continue from its tasks"
)
NUDGE_CFLOW2_MOVE_APPROVED = (
    "cflow2: the user approved your move request from the dashboard - the run "
    "moved where you asked; call the flow_status tool and continue from its tasks"
)
NUDGE_CFLOW2_REPORT_APPROVED = (
    "cflow2: the user granted your report request from the dashboard - the task "
    "was reported as you asked (its verify may still be running); call the "
    "flow_status tool and continue from its tasks"
)
NUDGE_CFLOW2_MOVE_DENIED = (
    "cflow2: the user refused your move request from the dashboard - the run "
    "stays where it was; call the flow_status tool, read the refusal "
    "(move_request) and continue on the declared route"
)
NUDGE_CFLOW2_REPORTED = (
    "cflow2: the user reported your run's task from the dashboard - stop working "
    "on it; call the flow_status tool and continue"
)
NUDGE_CFLOW2_ABORTED = (
    "cflow2: the user aborted your v2 workflow run from the dashboard - stop "
    "working on it; flow_status shows it aborted"
)


def _scope_profile(manager: SessionManager, scope: str) -> Optional[str]:
    try:
        return manager.get(cflow_clock.session_name(scope)).sdef.profile or None
    except Exception:  # noqa: BLE001 - a scope with no session: ambient caps
        return None


def _scope_home_mesh(request: web.Request, scope: str) -> Optional[str]:
    """The one team mesh the session a run scope names is in, or None (no
    session, none, several). A v2 run pins it at start so its asks keep
    routing there after the session joins another mesh (claunch-5itbf)."""
    from ..cflow2 import store as cflow2_store

    try:
        session = cflow2_store.session_of(cflow2_store.normalize_scope(scope))
        rows = [row for row in _mesh_mgr(request).meshes_for_session(session)
                if not row.get("parent_channel")]
    except Exception:  # noqa: BLE001 - no session behind the scope: nothing to pin
        return None
    return rows[0]["mesh"] if len(rows) == 1 else None


async def _cflow2_start(request: web.Request, cwd: str, scope: str, body: dict,
                        context: Optional[str]) -> Optional[web.Response]:
    """Start ``body['workflow']`` on the cflow2 runtime when it is a v2
    request (:func:`cflow2_web.choose`); ``None`` leaves it to v1. Like the
    v1 web start there is no force path: a live v2 run refuses it."""
    chosen = cflow2_web.choose(
        str(body.get("workflow") or ""), body.get("workflow_version"), cwd=cwd
    )
    if chosen is None:
        return None
    ref, chart = chosen
    params = cflow2_web.params_for(chart, body.get("workflow_params"), context or "")
    values = cflow2_web.check_inputs(chart, body.get("cflow_inputs"))  # InputsInvalid -> 400
    profile = _scope_profile(request.app["manager"], scope)
    mesh = _scope_home_mesh(request, scope)

    def start() -> dict:
        return cflow2_web.start_run(
            chart, inputs=values, cwd=cwd, scope=scope, params=params,
            caps=cflow2_runtime.resolve_caps(profile), mesh=mesh,
        )

    payload = await asyncio.to_thread(start)
    payload["version"] = 2
    payload["workflow_ref"] = ref
    payload["nudge_scheduled_sessions"] = _schedule_cflow_nudges(
        request.app, cwd, scope, NUDGE_CFLOW2_STARTED, force=True
    )
    return json_response(payload)


# --------------------------------------------------------------------------- #
# cflow v2: run page, the person's answers, definitions (docs/cflow-v2.md 9)
# --------------------------------------------------------------------------- #
def _cflow2_slot(request: web.Request) -> Tuple[str, str]:
    raw = request.query.get("cwd")
    if not raw:
        raise web.HTTPBadRequest(
            text=json.dumps({"error": "'cwd' query parameter required"}),
            content_type="application/json",
        )
    scope = request.query.get("scope") or cflow2_store.DEFAULT_SCOPE
    cflow2_store.normalize_scope(scope)  # StoreError -> 400
    return cflow_state.resolve_cwd(raw), scope


async def h_cflow2_run(request: web.Request) -> web.Response:
    """A v2 run: ``run`` (runtime status), ``chart`` (the IR's display
    fields; task prompts with ``prompts=1``), the journal tail (200), the
    live instances, per-node visits and the delegation rows."""
    cwd, scope = _cflow2_slot(request)
    prompts = request.query.get("prompts") in ("1", "true", "yes")
    data = await asyncio.to_thread(cflow2_web.run_detail, cwd, scope, prompts)
    data["sessions"] = _scope_sessions(request.app["manager"], cwd, scope)
    data["collections"] = (
        await asyncio.to_thread(_cflow2_collections, cwd, scope) if data.get("run") else {}
    )
    data["trigger_events"] = [
        e for e in data.get("journal") or [] if str(e.get("event") or "") in CFLOW2_TRIGGER_EVENTS
    ][-CFLOW2_TRIGGER_TAIL:]
    return json_response(data)


async def h_cflow2_node(request: web.Request) -> web.Response:
    """One node of a v2 run in full, for the run page's node pane
    (cflow2_web.node_detail): ``?cwd=&scope=&node=<id>``."""
    cwd, scope = _cflow2_slot(request)
    node = request.query.get("node") or ""
    data = await asyncio.to_thread(cflow2_web.node_detail, cwd, scope, node)
    if data is None:
        return json_response({"error": f"no node {node!r} in this run"}, status=404)
    return json_response(data)


#: Journal events the v2 run view lists under "Triggers" (docs 4.5).
CFLOW2_TRIGGER_EVENTS = ("trigger_queued", "trigger_done", "trigger_skipped")
CFLOW2_TRIGGER_TAIL = 50


def _cflow2_collections(cwd: str, scope: str) -> dict:
    """``runtime.collection_view`` for the run payload: ``{name: {kind,
    items, ...}}``; ``{}`` when the run declares none or it cannot be read."""
    view_fn = getattr(cflow2_runtime, "collection_view", None)
    if view_fn is None:
        return {}
    try:
        view = view_fn(cwd, scope)
    except _CFLOW2_ERRORS:
        return {}
    except Exception:  # noqa: BLE001 - the run view must still render
        log.exception("cflow2 collection view of %s/%s failed", cwd, scope)
        return {}
    if isinstance(view, dict) and isinstance(view.get("collections"), dict):
        view = view["collections"]
    if not isinstance(view, dict):
        return {}
    # The declared status values, for the person's "set status" control.
    try:
        specs = (cflow2_store.RunStore(cwd, scope).load_chart().meta or {}).get("collections") or {}
    except Exception:  # noqa: BLE001 - the table renders without them
        specs = {}
    for name, entry in view.items():
        status = (specs.get(name) or {}).get("status") if isinstance(specs, dict) else None
        if isinstance(entry, dict) and isinstance(status, dict) and status.get("values"):
            entry.setdefault("status_values", list(status["values"]))
    return view


async def h_cflow2_collection(request: web.Request) -> web.Response:
    """A person's change to a v2 run's collection (principal ``person``,
    docs 2.7): ``op`` ``set`` (``key``, ``fields``) or ``remove`` (``key``).
    The collection's ``access`` decides. Body: cwd, scope, op, collection,
    key, fields. Answers the run's collections after the change."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    op = str(body.get("op") or "")
    if op not in ("set", "remove"):
        return json_error(400, "'op' must be 'set' or 'remove'")
    name = str(body.get("collection") or "").strip()
    key = body.get("key")
    if not name or key in (None, ""):
        return json_error(400, "'collection' and 'key' are required")
    fields = body.get("fields")
    if op == "set" and (not isinstance(fields, dict) or not fields):
        return json_error(400, "'set' takes 'fields' (an object of field -> value)")
    await asyncio.to_thread(
        cflow2_runtime.collection_op, op, name, cwd=cwd, scope=scope,
        principal="person", session=cflow2_runtime.CALLER_USER, key=key,
        fields=fields if op == "set" else None,
    )
    collections = await asyncio.to_thread(_cflow2_collections, cwd, scope)
    return json_response({"status": "ok", "op": op, "collection": name, "key": key,
                          "collections": collections})


async def h_cflow2_runs(request: web.Request) -> web.Response:
    """Every v2 run in the directories live sessions stand in (plus an
    explicit ``?cwd=``): one summary line each -- status and the labels of
    the active tasks."""
    manager: SessionManager = request.app["manager"]
    cwds = []
    if request.query.get("cwd"):
        cwds.append(cflow_state.resolve_cwd(request.query["cwd"]))
    for session in manager.list():
        cwd = _session_cwd(session)
        if cwd and cwd not in cwds:
            cwds.append(cwd)
    runs = await asyncio.to_thread(cflow2_web.list_runs, cwds)
    return json_response({"runs": runs})


async def h_cflow2_rail(request: web.Request) -> web.Response:
    """The session rail's v2 lines: ``{"rows": {session: entry}}`` for each
    registered session whose own directory holds a v2 run under the session's own
    name (``cflow2_web.rail_entry``). Its own endpoint rather than a field of
    ``/api/cflow`` or ``/api/cflow2/runs``: the rail asks one question per
    session, keyed by the session, not by the run slots on disk."""
    manager: SessionManager = request.app["manager"]
    pairs = []
    for session in manager.list():
        cwd = _session_cwd(session)
        if cwd:
            pairs.append((session.sdef.name, cwd))
    rows = await asyncio.to_thread(cflow2_web.rail_entries, pairs)
    return json_response({"rows": rows})


async def h_cflow2_definition(request: web.Request) -> web.Response:
    """One v2 definition, read-only: compiled tree, static check findings,
    source text. ``?name=`` (and ``?cwd=`` for the project layer)."""
    name = str(request.query.get("name") or "").strip()
    if not name:
        return json_error(400, "'name' query parameter required")
    if not cflow2_layers.is_name(name):
        # Name only (docs/cflow-v2.md 1): no path reads a file outside the layers.
        return json_error(400, f"'name' must be a v2 definition name, not a path: {name!r}")
    cwd = cflow_state.resolve_cwd(request.query.get("cwd"))
    try:
        data = await asyncio.to_thread(cflow2_web.definition_view, name, cwd)
    except cflow2_web.ChoiceError as exc:
        return json_error(404, str(exc))
    return json_response(data)


async def h_cflow2_inputs_check(request: web.Request) -> web.Response:
    """Check start inputs without starting (the new-session form calls it as
    the user edits): per-field errors, cross-input constraints included
    (docs/cflow-v2.md 2.6). Body: workflow, workflow_version?, cwd?,
    cflow_inputs. 200 ``{ok, values, errors}``; 404 when the workflow is not
    a startable v2 definition."""
    try:
        body = await request.json()
    except Exception:  # noqa: BLE001 - malformed body
        return json_error(400, "JSON body required")
    if not isinstance(body, dict):
        return json_error(400, "JSON body must be an object")
    workflow = str(body.get("workflow") or "").strip()
    if not workflow:
        return json_error(400, "'workflow' required in the JSON body")
    cwd = cflow_state.resolve_cwd(body.get("cwd"))

    def run() -> Optional[dict]:
        chosen = cflow2_web.choose(workflow, body.get("workflow_version"), cwd=cwd)
        if chosen is None:
            return None
        return cflow2_web.inputs_check(chosen[1], body.get("cflow_inputs"))

    try:
        out = await asyncio.to_thread(run)
    except cflow2_web.ChoiceError as exc:
        return json_error(404, str(exc))
    if out is None:
        return json_error(404, f"{workflow!r} is not a v2 workflow")
    return json_response(out)


async def h_cflow2_select(request: web.Request) -> web.Response:
    """Answer a v2 ``choose`` from the dashboard, as the user: a
    ``chooser: user`` choice is accepted here and refused to the agent's
    flow_select. Body: cwd, scope, option, node (instance or label; optional
    when one choice is open), reason."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    option = str(body.get("option") or "")
    if not option:
        return json_error(400, "'option' required in the JSON body")
    node = str(body.get("node") or "") or None
    reason = str(body.get("reason") or "") or None
    payload = await asyncio.to_thread(
        cflow2_runtime.select, node, option, reason,
        caller=cflow2_runtime.CALLER_USER, cwd=cwd, scope=scope,
    )
    words = cflow2_runtime.person_pick_words(payload, option)
    payload["nudge_scheduled_sessions"] = await _nudge_sessions(
        request.app["manager"], cwd, scope,
        NUDGE_CFLOW2_PICKED.format(option=option, **words),
    )
    return json_response(payload)


async def h_cflow2_extend(request: web.Request) -> web.Response:
    """Answer a v2 ``loop_limit`` hold: allow ``by`` more iterations,
    ``reset`` the loop's count (it gets its full ``max`` again), or ``stop``
    the loop (it finishes ``fail``). With ``visits: true`` it answers a
    ``visit_limit`` hold instead (``runtime.extend_visits``: ``by`` more
    visits of the node over the run, or ``stop``; no reset). Body: cwd,
    scope, node (optional with one hold), by, stop, reset, visits."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    node = str(body.get("node") or "") or None
    try:
        by = int(body.get("by") or 1)
    except (TypeError, ValueError):
        return json_error(400, "'by' must be a whole number")
    stop = bool(body.get("stop"))
    reset = bool(body.get("reset"))
    if reset and body.get("visits"):
        return json_error(400, "'reset' answers a loop limit, not a visit limit")
    # ``visits: true`` answers a ``visit_limit`` hold (the chart's
    # max_visits) instead of a loop's ``loop_limit``.
    if body.get("visits"):
        extend = functools.partial(cflow2_runtime.extend_visits, node, by, stop=stop)
    else:
        extend = functools.partial(cflow2_runtime.extend_loop, node, by, stop=stop,
                                   reset=reset)
    payload = await asyncio.to_thread(extend, cwd=cwd, scope=scope)
    payload["nudge_scheduled_sessions"] = await _nudge_sessions(
        request.app["manager"], cwd, scope, NUDGE_CFLOW2_ANSWERED
    )
    return json_response(payload)


async def h_cflow2_approve(request: web.Request) -> web.Response:
    """Answer a v2 task's ``ask`` from the dashboard, as the user. Body:
    cwd, scope, node (optional with one open ask), decision (``approve``,
    ``decline`` or a decision ask's option name), reason. The handler takes
    the run lock and calls back into this API to withdraw and nudge, so it
    runs off the event loop."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    payload = await asyncio.to_thread(cflow2_approve.approve, cwd, scope, body)
    payload["nudge_scheduled_sessions"] = await _nudge_sessions(
        request.app["manager"], cwd, scope, NUDGE_CFLOW2_ANSWERED
    )
    return json_response(payload)


async def h_cflow2_nudge(request: web.Request) -> web.Response:
    """Type the v2 continue line into the run's session (the run page's
    Nudge). Body: cwd, scope."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, _ = resolved
    nudged = await _nudge_sessions(request.app["manager"], cwd, scope, NUDGE_CFLOW2_CONTINUE)
    return json_response({"ok": True, "nudge_scheduled_sessions": nudged})


async def h_cflow2_abort(request: web.Request) -> web.Response:
    """Abort the live v2 run, as the user -- the dashboard half of
    ``claunch cflow2 abort``. Body: cwd, scope, reason."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    reason = str(body.get("reason") or "").strip() or "aborted from the dashboard"
    payload = await asyncio.to_thread(cflow2_runtime.abort, reason, cwd=cwd, scope=scope)
    payload["nudge_scheduled_sessions"] = await _nudge_sessions(
        request.app["manager"], cwd, scope, NUDGE_CFLOW2_ABORTED
    )
    return json_response(payload)


async def h_cflow2_archive(request: web.Request) -> web.Response:
    """Move a finished v2 run into the archive, leaving the slot idle (the
    run page then offers a start). A live run is refused. Body: cwd, scope."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, _ = resolved
    payload = await asyncio.to_thread(cflow2_runtime.archive, cwd=cwd, scope=scope)
    return json_response(payload)


async def h_cflow2_report(request: web.Request) -> web.Response:
    """A person forces an active task's outcome (docs 2.11): the result is
    checked against the task's ``returns`` and routed like the driver's
    report, journaled ``by: user``. Body: cwd, scope, node, outcome, value,
    reason."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    outcome = str(body.get("outcome") or "")
    if not outcome:
        return json_error(400, "'outcome' required in the JSON body")
    node = str(body.get("node") or "") or None
    reason = str(body.get("reason") or "").strip()
    text = "forced by a person from the dashboard" + (f": {reason}" if reason else "")
    payload = await asyncio.to_thread(
        cflow2_runtime.report, node, outcome, body.get("value"), text,
        cwd=cwd, scope=scope, by=cflow2_runtime.CALLER_USER,
    )
    payload["nudge_scheduled_sessions"] = await _nudge_sessions(
        request.app["manager"], cwd, scope, NUDGE_CFLOW2_REPORTED
    )
    return json_response(payload)


async def h_cflow2_move(request: web.Request) -> web.Response:
    """A person moves the run to a ``force_target`` node (docs 2.11). Body:
    cwd, scope, node (the target's id or label), reason."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    target = str(body.get("node") or "")
    if not target:
        return json_error(400, "'node' required in the JSON body")
    reason = str(body.get("reason") or "").strip() or None
    payload = await asyncio.to_thread(
        cflow2_runtime.force_move, target, reason, cwd=cwd, scope=scope,
    )
    payload["nudge_scheduled_sessions"] = await _nudge_sessions(
        request.app["manager"], cwd, scope, NUDGE_CFLOW2_FORCED
    )
    return json_response(payload)


async def h_cflow2_step_out(request: web.Request) -> web.Response:
    """Release an exact live container frame and continue its parent (user UI)."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    instance = body.get("instance")
    if not isinstance(instance, str) or not instance:
        return json_error(400, "'instance' required in the JSON body")
    run = body.get("run")
    if not isinstance(run, str) or not run:
        return json_error(400, "'run' required in the JSON body")
    payload = await asyncio.to_thread(
        cflow2_runtime.step_out, instance, body.get("outcome", "ok"), body.get("value"),
        str(body.get("reason") or "").strip(), cwd=cwd, scope=scope,
        expected_run=run,
    )
    payload["nudge_scheduled_sessions"] = await _nudge_sessions(
        request.app["manager"], cwd, scope, NUDGE_CFLOW2_FORCED
    )
    return json_response(payload)


async def h_cflow2_move_resolve(request: web.Request) -> web.Response:
    """A person answers the driver's move request (``flow_request_move``) --
    the dashboard half of ``claunch cflow2 move --approve|--deny``. Body:
    cwd, scope, decision (approve | deny), reason. The driver is nudged
    either way: an approval moved its run (or, for a report request,
    reported its task), a refusal released it."""
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    decision = str(body.get("decision") or "").strip().lower()
    if decision not in ("approve", "deny"):
        return json_error(400, "'decision' must be 'approve' or 'deny'")
    reason = str(body.get("reason") or "").strip() or None
    payload = await asyncio.to_thread(
        cflow2_runtime.resolve_move, decision, reason=reason, cwd=cwd,
        scope=scope,
    )
    payload["nudge_scheduled_sessions"] = await _nudge_sessions(
        request.app["manager"], cwd, scope,
        NUDGE_CFLOW2_MOVE_DENIED if decision != "approve"
        else NUDGE_CFLOW2_REPORT_APPROVED
        if (payload.get("move_request") or {}).get("kind") == cflow2_runtime.MOVE_KIND_REPORT
        else NUDGE_CFLOW2_MOVE_APPROVED,
    )
    return json_response(payload)


async def h_cflow2_asks(request: web.Request) -> web.Response:
    """Open asks of one v2 run (``cflow2_approve.pending``)."""
    cwd, scope = _cflow2_slot(request)
    data = await asyncio.to_thread(cflow2_approve.pending, cwd, scope)
    return json_response(data)


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


async def h_cflow_state_set(request: web.Request) -> web.Response:
    """Write one of the run's declared state paths from the dashboard.

    The web face of ``claunch cflow set``: the same engine write, as the
    person (``by="user"``) -- the dashboard token is the person's channel,
    as it is for approve. The path's ``editable:`` declaration still decides:
    an agent-only path is refused here exactly as it is on the CLI. The
    driving session is nudged so it reads the value from its payload now.
    """
    resolved, err = await _cflow_action_cwd(request)
    if err:
        return err
    cwd, scope, body = resolved
    path = str(body.get("path") or "").strip()
    if not path:
        return json_error(400, "'path' required in the JSON body")
    if "value" not in body:
        return json_error(400, "'value' required in the JSON body")
    value = body.get("value")
    payload = cflow_engine.set_state(path, value, by="user", cwd=cwd, scope=scope)
    if payload.get("changed"):
        payload["nudge_scheduled_sessions"] = await _nudge_sessions(
            request.app["manager"], cwd, scope,
            cflow_engine.nudge_for_state_write(payload["path"], payload["value"]),
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
    # ``?project=`` narrows to one project's meshes; absent = every mesh.
    project_filter = str(request.query.get("project") or "").strip()
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
                if projects.matches(m.project, project_filter)
            ],
            "outgoing": mm.outgoing_list(),
            "relay": request.app["relay_state"](),
        }
    )


async def h_mesh_create(request: web.Request) -> web.Response:
    body = await _json_body(request)
    mesh = _mesh_mgr(request).create(
        str(body.get("name") or ""), project=str(body.get("project") or "")
    )
    return json_response(_mesh_mgr(request).mesh_info(mesh), status=201)


async def h_mesh_get(request: web.Request) -> web.Response:
    mm = _mesh_mgr(request)
    mesh = mm.get(request.match_info["mesh"])
    # `?session=` asks "which member am I?" — answered as `you`. Optional, so
    # the dashboard poll (which is nobody's session) is unchanged.
    session = str(request.query.get("session") or "")
    # `?state=` is the roster filter, applied before the expensive half of
    # the answer is built rather than after it reaches the page. Absent means
    # every member, which is what this route has always sent.
    state = str(request.query.get("state") or "all")
    if not mesh_mod.valid_member_state(state):
        return json_error(400, f"invalid member state: {state!r}")
    return json_response(
        {**mm.mesh_info(mesh, session=session, state=state),
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
    [...]}`` for the whole list. The primary role is not edited here:
    :func:`h_mesh_member_roles` takes it, so a caller that meant only the
    subroles cannot move the primary by a stray field.
    """
    body = await _json_body(request)
    if "role" in body:
        return json_error(
            400, "the primary 'role' is not edited here — PATCH "
            ".../members/<handle>/roles changes it; send 'add', 'remove' or "
            "'set' for the subroles"
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


async def h_mesh_member_roles(request: web.Request) -> web.Response:
    """Change one live member's roles: its primary and/or its subroles.

    Body: ``{"role": "leader"}`` for the primary, and ``add``/``remove`` or
    ``set`` for the subroles as :func:`h_mesh_member_subroles` takes them;
    any combination in one call, applied as one change. The member is typed
    a notice of the change, with the new stance when the primary moved.
    """
    body = await _json_body(request)
    fields = ("role", "add", "remove", "set")
    if not any(k in body for k in fields):
        return json_error(
            400, "send 'role' for the primary and/or 'add', 'remove' or "
            "'set' for the subroles"
        )
    role = body.get("role")
    if role is not None and not isinstance(role, str):
        return json_error(400, "'role' is one role name")
    replace = None
    if "set" in body:
        replace = _subroles_in({"subroles": body.get("set")})
    member = _mesh_mgr(request).set_member_roles(
        request.match_info["mesh"],
        request.match_info["handle"],
        role=role,
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


async def _mesh_audience(
    manager: SessionManager, board, session: str, status: str
) -> List[str]:
    """The sessions assigned an issue in ``status`` on ``session``'s board.

    The board is the one the sender's directory files on — the leader's
    selector means *its* landing queue, not every board the daemon serves.
    """
    try:
        cwd = manager.get(session).sdef.cwd
    except ManagerError:
        raise MeshError(
            f"audience selectors need a sender session on this daemon; "
            f"{session!r} is not one"
        ) from None
    try:
        root = await board.root_for(cwd)
        if root is None:
            raise MeshError(f"{session!r} files on no board ({cwd})")
        rows = await board.issues(root)
    except BeadsError as exc:
        raise MeshError(f"the board could not be read: {exc}") from None
    return sorted({
        str(r.get("assignee")) for r in rows
        if r.get("status") == status and r.get("assignee")
    })


async def h_mesh_send(request: web.Request) -> web.Response:
    body = await _json_body(request)
    sender = str(body.get("from") or "")
    to = body.get("to")
    text = body.get("body")
    if not sender:
        return json_error(400, "'from' required (a handle or a session name)")
    if not isinstance(to, (str, list)) or not to:
        return json_error(
            400, "'to' must be '*', an audience selector (@in_review), a "
            "handle, or a list of handles"
        )
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


async def h_mesh_urgent(request: web.Request) -> web.Response:
    """The exception path to a member the sender is not connected to.

    ``from`` is the caller's own session (empty: the human operator). A body
    ``external`` flag is not read — see :meth:`MeshManager.urgent_send`.
    """
    body = await _json_body(request)
    to = body.get("to")
    text = body.get("body")
    if not isinstance(to, str) or not to:
        return json_error(400, "'to' must be one handle or session name")
    if not isinstance(text, str):
        return json_error(400, "'body' must be a string")
    result = _mesh_mgr(request).urgent_send(
        request.match_info["mesh"],
        str(body.get("from") or ""),
        to,
        text,
        reason=str(body.get("reason") or ""),
        target_mesh=str(body.get("target_mesh") or ""),
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
    """Who has been asked something and answered nothing — per message.

    ``?state=`` narrows the roster before the ledger is built, the same way
    and for the same cost as on the mesh itself (see :func:`h_mesh_get`).
    ``?handle=`` narrows it to one member, which overrides ``state`` so that
    a member asked for by name is answered for whatever its session's state.
    """
    mm = _mesh_mgr(request)
    state = str(request.query.get("state") or "all")
    if not mesh_mod.valid_member_state(state):
        return json_error(400, f"invalid member state: {state!r}")
    handle = request.query.get("handle") or None
    return json_response(
        mm.owed_report(
            mm.get(request.match_info["mesh"]), state=state, handle=handle
        )
    )


async def h_mesh_flows(request: web.Request) -> web.Response:
    """What every member of this mesh is *doing*: its cflow run, and the
    workflow graph that run is walking.

    The roster says who is in the room and who may speak to whom; this says
    where each of them has got to. Kept off ``/api/mesh/{mesh}`` on purpose —
    that payload is polled by a page which does not need the graphs, and a
    workflow snapshot is an order of magnitude bigger than a member row.

    Graphs are deduplicated by what they contain, not by where they came
    from. ``workflow@cwd`` was the key, and a mesh whose members each stand
    in their own worktree defeats it entirely: one workflow file, copied into
    73 trees, shipped 73 identical 247KB graphs -- 16.3MB of a 11.9MB answer
    (mesh-0826, 2026-09-20). The digest collapses those to one and still
    separates two runs of the same name over an edited YAML, which is what
    the directory in the old key was there to do.

    ``state`` narrows the roster first, the same word :func:`h_mesh_get`
    takes: a lane is drawn per member, and a member whose session has ended
    has a run nobody is driving.

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
    state = str(request.query.get("state") or "all")
    if not mesh_mod.valid_member_state(state):
        return json_error(400, f"invalid member state: {state!r}")
    manager: SessionManager = request.app["manager"]
    known = {s.sdef.name: s for s in manager.list()}

    flows: dict = {}
    workflows: dict = {}
    #: workflow@cwd -> digest, so one tree's snapshot is read once per poll
    #: even though several members may stand in it.
    digests: dict = {}
    for handle in sorted(mesh.members):
        member = mesh.members[handle]
        if not mesh_mod.member_in_state(mm.member_category(mesh, member), state):
            continue
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
        where = f"{name}@{cwd}"
        if where not in digests:
            try:
                graph = _serialize_workflow(
                    cflow_state.load_snapshot(cwd, member.session)
                )
            except (WorkflowError, StateError, OSError) as exc:
                # A missing or unreadable snapshot costs the track, not the
                # card: status, step and blockage all still read.
                digests[where] = None
                flows[handle]["graph_error"] = str(exc)
                continue
            digest = hashlib.sha256(
                json.dumps(graph, sort_keys=True, default=str).encode("utf-8")
            ).hexdigest()[:16]
            digests[where] = digest
            workflows.setdefault(digest, graph)
        if digests[where] is None:
            flows[handle]["graph_error"] = "workflow snapshot unreadable"
            continue
        flows[handle]["key"] = digests[where]
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


def _describe_workspaces() -> list:
    """Every registered workspace as :func:`reposync.describe` sees it. A
    workspace that is not a readable git repository is listed with its error,
    so the asker can tell "not here" from "here but broken"."""
    rows = []
    for ws in workspaces.list_all():
        try:
            doc = reposync.describe(reposync.toplevel(ws.path))
        except reposync.SyncError as exc:
            rows.append({"workspace": ws.name, "error": str(exc)})
            continue
        rows.append({"workspace": ws.name, **doc})
    return rows


async def h_peer_repos(request: web.Request) -> web.Response:
    return json_response({"repos": await asyncio.to_thread(_describe_workspaces)})


async def h_reposync_repos(request: web.Request) -> web.Response:
    """This daemon's own answer to /peer/repos, for the CLI and the web UI."""
    return json_response({
        "repos": await asyncio.to_thread(_describe_workspaces),
        "relay": request.app["relay_state"](),
    })


async def h_relay_peer_repos(request: web.Request) -> web.Response:
    mm = _mesh_mgr(request)
    if mm.peer_transport is None:
        return json_error(400, "relay uplink is not running")
    machine = request.match_info["machine"]
    try:
        payload = await mm.peer_transport(machine, "/peer/repos", {})
    except Exception as exc:  # noqa: BLE001 — PeerError, an old peer's 404
        return json_error(502, f"{machine}: {exc}")
    return json_response({"machine": machine, "repos": payload.get("repos", [])})


#: How long one peer gets to answer /peer/repos before the Sync panel
#: reports it unreadable and plans without it.
PEER_REPOS_TIMEOUT = 15.0


async def _peer_repos_all(
    request: web.Request,
) -> Tuple[Dict[str, list], List[str], List[str]]:
    """Every relay peer's registered repositories, one line per peer that
    could not be read, and the names of those peers. Peers are advisory: no
    relay, or a peer whose daemon predates reposync, narrows what the panel
    shows and nothing else."""
    mm = _mesh_mgr(request)
    if mm.peer_lister is None or mm.peer_transport is None:
        return {}, ["relay uplink is not running -- peers not consulted"], []
    try:
        names = await mm.peer_lister()
    except Exception as exc:  # noqa: BLE001 — PeerError
        return {}, [f"cannot list relay peers ({exc})"], []
    me = (request.app["relay_state"]() or {}).get("name")
    found: Dict[str, list] = {}
    problems: List[str] = []
    unreadable: List[str] = []

    async def one(machine: str) -> None:
        try:
            payload = await asyncio.wait_for(
                mm.peer_transport(machine, "/peer/repos", {}), PEER_REPOS_TIMEOUT
            )
        except Exception as exc:  # noqa: BLE001 — PeerError, timeout, an old peer's 404
            problems.append(f"{machine}: cannot read its repositories ({exc or type(exc).__name__})")
            unreadable.append(machine)
            return
        found[machine] = list(payload.get("repos") or [])

    await asyncio.gather(*(one(m) for m in sorted(names) if m != me))
    return found, sorted(problems), sorted(unreadable)


def _ws_path(request: web.Request) -> Tuple[Optional[str], Optional[web.Response]]:
    name = request.match_info["name"]
    ws = workspaces.find(name)
    if ws is None or ws.name != name:
        return None, json_error(404, f"no workspace named {name!r}")
    return ws.path, None


def _machine_name(request: web.Request) -> str:
    """This machine as the shared remote's branch namespace knows it: the
    relay name when there is one, as the CLI uses (reposync_flow.relay_identity)."""
    return str((request.app["relay_state"]() or {}).get("name") or socket.gethostname())


def _ws_usage_doc(app: web.Application) -> dict:
    tasks = app["ws_usage_tasks"]
    cached = workspace_usage.load()
    out = {}
    for w in workspaces.list_all():
        row = dict(cached.get(w.name) or {})
        if w.name in tasks:
            row["running"] = True
        if row:
            out[w.name] = row
    return {"workspaces": out}


async def h_workspace_usage(request: web.Request) -> web.Response:
    """The Workspaces page's size badges (claunch-2oait): each workspace's
    last measurement with the time it was taken, and ``running`` while one
    is being taken. Reads the cache only; nothing is walked here."""
    return json_response(await asyncio.to_thread(_ws_usage_doc, request.app))


async def h_workspace_usage_measure(request: web.Request) -> web.Response:
    """Measure one workspace's repository and worktrees in the background
    (workspace_usage.measure) and keep the answer. Answers at once (202);
    a measurement already running for it is not started twice."""
    path, err = _ws_path(request)
    if err is not None:
        return err
    name = request.match_info["name"]
    tasks = request.app["ws_usage_tasks"]
    if name not in tasks:
        tasks[name] = asyncio.ensure_future(_ws_usage_run(tasks, name, path))
    return json_response({"name": name, "running": True}, status=202)


async def _ws_usage_run(tasks: dict, name: str, path: str) -> None:
    def now() -> str:
        return datetime.now(timezone.utc).isoformat(timespec="seconds")

    try:
        doc = await asyncio.to_thread(workspace_usage.measure, path)
        await asyncio.to_thread(workspace_usage.save, name, {**doc, "at": now()})
    except Exception as exc:  # the row says it failed; the last number stays
        log.warning("disk usage of workspace %s failed: %s", name, exc)
        prev = (await asyncio.to_thread(workspace_usage.load)).get(name) or {}
        try:
            await asyncio.to_thread(workspace_usage.save, name,
                                    {**prev, "error": str(exc), "failed_at": now()})
        except OSError:
            pass
    finally:
        tasks.pop(name, None)


async def h_reposync_ws_summary(request: web.Request) -> web.Response:
    """The Workspaces page's row badges: branch, ahead/behind and changed
    files for every registered workspace that is there, read from local git
    only (claunch-gax44). One ``git status`` per workspace, run side by side
    off the loop; a workspace that is missing is left out, one git cannot
    read carries ``error``."""
    present = [w for w in workspaces.list_all() if w.exists()]
    docs = await asyncio.gather(*(
        asyncio.to_thread(reposync_flow.git_summary, w.path) for w in present
    ))
    return json_response({"workspaces": {w.name: d for w, d in zip(present, docs)}})


async def h_reposync_ws_status(request: web.Request) -> web.Response:
    """The Sync panel's view of one workspace: remotes, the peers sharing an
    address with it, and the plan a sync of its default branch would carry
    out (``?fetch=0`` plans against the remote-tracking refs as they are)."""
    path, err = _ws_path(request)
    if err is not None:
        return err
    name = request.match_info["name"]
    do_fetch = request.query.get("fetch", "1") not in ("0", "false", "no")
    if request.query.get("peers", "1") in ("0", "false", "no"):
        peers, problems, unreadable = {}, [], []
    else:
        peers, problems, unreadable = await _peer_repos_all(request)
    doc = await asyncio.to_thread(
        reposync_flow.workspace_status, path, name, peers, problems,
        do_fetch=do_fetch, remote=request.query.get("remote") or None,
        branch=request.query.get("branch") or None, machine=_machine_name(request),
    )
    if doc.get("repo"):
        doc["remote_comparisons"] = await asyncio.to_thread(
            reposync_flow.remote_comparisons, doc["repo"],
            branch=request.query.get("branch") or (doc.get("plan") or {}).get("branch"),
            do_fetch=do_fetch,
            already_fetched=(doc.get("plan") or {}).get("remote") if do_fetch else None,
        )
    doc["machine"] = _machine_name(request)
    # the peers table shows these as unreadable rows (claunch-gjjdp)
    doc["unreadable"] = unreadable
    agent = reposync_agent.active(request.app["manager"], doc["repo"]) if doc.get("repo") else None
    doc["agent"] = reposync_agent.summary(agent) if agent else None
    # The picker labels each candidate with its meshes and its role in each,
    # so a parent is chosen by what it is on the team (claunch-7ez8s).
    mesh_mgr = request.app["mesh"]
    doc["agent_parents"] = [
        {"name": s.sdef.name, "profile": s.sdef.profile,
         "held": bool(s.exited and getattr(s, "held_at", None)),
         "meshes": [{"mesh": m["mesh"], "role": m["role"]}
                    for m in mesh_mgr.meshes_for_session(s.sdef.name)]}
        for s in request.app["manager"].list()
        if _ws_parent_usable(s)
    ]
    return json_response(doc)


def _ws_authorize(request: web.Request, card: dict, rid: str) -> Optional[web.Response]:
    """Whether a push the Sync panel asked for may run now. ``None`` means
    go; anything else is the answer to send instead.

    The panel's button is NOT the approval (claunch-tqm2e, s469's decline of
    d641c8fe): any holder of the daemon token -- a session included -- can
    POST here, so a push waits behind the same gate a session's CLI push
    does (:mod:`daemon.reposync_gate`, ``daemon.reposync_push``). The first
    call files the card and answers 202 with the request id; a later call
    naming that id runs only once a person approved it on the card AND the
    plan still has the tips the card showed (:func:`reposync_flow.approval_matches`).
    Rejected and expired requests refuse; nothing is pushed on any of them."""
    if reposync_flow.approval_mode() == reposync_flow.AUTO:
        return None
    gate = request.app["reposync_gate"]
    if not rid:
        record = gate.submit(session="", plan=card)
        return json_response({
            "status": "waiting_approval", "request": record["id"],
            "deadline": record["deadline"], "plan": card,
        }, status=202)
    record = gate.get(rid)
    if record is None:
        return json_error(404, f"no reposync request {rid!r}")
    if record.get("status") == reposync_gate.PENDING:
        return json_response({
            "status": "waiting_approval", "request": rid,
            "deadline": record.get("deadline"), "plan": card,
        }, status=202)
    if record.get("status") != reposync_gate.APPROVED:
        return json_response({
            "error": f"reposync request {rid} was {record.get('status')}; nothing was pushed",
            "status": record.get("status"), "request": rid, "plan": card,
        }, status=409)
    if not reposync_flow.approval_matches(record, card):
        return json_response({
            "error": "the branch or the remote moved after the approval; "
                     "nothing was pushed -- ask again",
            "status": "stale_approval", "request": rid, "plan": card,
        }, status=409)
    return None


def _ws_plan(path: str, name: str, body: dict, *, do_fetch: bool = True):
    repo = reposync.toplevel(path)
    remote = str(body.get("remote") or "")
    if not remote:
        raise reposync.SyncError("no remote named -- read the status first")
    p = reposync.plan(repo, remote, str(body.get("branch") or "") or None, do_fetch=do_fetch)
    key = reposync.url_key(reposync.remotes(repo).get(remote, ""))
    return p, reposync_flow.plan_card(p, workspace=name, key=key)


def _ws_execute(p: "reposync.Plan", card: dict) -> dict:
    # the daemon has no session scratch of its own; its rebase worktrees live
    # under the daemon directory, never in the workspace
    result = reposync.execute(p, scratch=str(paths.daemon_dir() / "reposync-scratch"))
    reposync.git(p.repo, "config", reposync.REMOTE_CONFIG, p.remote, check=False)
    board = []
    if result.rewritten:
        board = reposync_flow.annotate_board(p.repo, result.rewritten, remote=p.remote, branch=p.branch)
    return {"ok": True, "plan": card, **result.to_dict(), "board": board}


async def h_reposync_ws_agent_preview(request: web.Request) -> web.Response:
    """Read-only assignment preview; use the same task builder as child spawn."""
    path, err = _ws_path(request)
    if err is not None:
        return err
    body = await _json_body(request)
    name = request.match_info["name"]
    try:
        if body.get("kind") == "sync":
            p, card = await asyncio.to_thread(_ws_plan, path, name, body, do_fetch=False)
            if p.local != body.get("local") or p.upstream != body.get("upstream"):
                return json_response({"error": "The plan changed; refresh Sync before previewing.",
                                      "stale": True, "plan": card}, status=409)
        elif body.get("kind") == "push":
            card = await asyncio.to_thread(_ws_publish_card, path, name, body, _machine_name(request))
        else:
            return json_error(400, "kind must be sync or push")
    except reposync.SyncError as exc:
        return json_error(409, str(exc))
    mode = body.get("agent_mode", "agent")
    if mode not in reposync_agent.WORKFLOWS:
        return json_error(400, "agent_mode must be agent or on_failure")
    return json_response({"prompt": reposync_agent.task(card, mode=mode), "plan": card,
                          "workflow": reposync_agent.WORKFLOWS[mode]})


async def _ws_sync_serialized(request: web.Request, handler) -> web.Response:
    path, err = _ws_path(request)
    if err is not None:
        return err
    key = os.path.normcase(os.path.realpath(path))
    lock = request.app["ws_sync_locks"].setdefault(key, asyncio.Lock())
    async with lock:
        return await handler(request)


def _ws_agent_options(request: web.Request, body: dict) -> str:
    mode = body.get("agent_mode", "commands")
    if mode not in ("commands", "agent", "on_failure"):
        raise ValueError("agent_mode must be commands, agent or on_failure")
    if mode != "commands":
        parent = body.get("agent_parent")
        if not isinstance(parent, str) or not parent:
            raise ValueError("select a parent session for the sync agent")
        session = request.app["manager"].get(parent)
        if not _ws_parent_usable(session):
            raise ValueError("the sync agent needs a live agent parent session")
    return mode


def _ws_parent_usable(session) -> bool:
    """Whether ``session`` may parent a sync agent: an agent session that is
    live or held. A held one is woken before the child is staged
    (:func:`_ws_start_agent`, claunch-8tvq7)."""
    if session.sdef.harness == "shell":
        return False
    return not session.exited or bool(getattr(session, "held_at", None))


def _ws_active_agent(request: web.Request, card: dict, mode: str):
    agent = reposync_agent.active(request.app["manager"], card["repo"])
    if not agent:
        return None
    doc = {"status": "delegated", "agent": reposync_agent.summary(agent), "plan": card}
    if mode == "agent":
        return json_response(doc, status=202)
    return json_response({**doc, "error": "a sync agent is already assigned; open its session"},
                         status=409)


async def _ws_start_agent(request: web.Request, card: dict, body: dict, *, error: str = ""):
    """Use normal spawn policy, board onboarding and launch; never approve a push."""
    manager = request.app["manager"]
    parent = body["agent_parent"]
    assignment = reposync_agent.assignment(card, mode=body["agent_mode"], error=error)
    assignment["workspace"] = request.match_info["name"]
    warnings = []
    child = None
    try:
        # A held parent comes back first: staging refuses an exited parent,
        # and the child reports to it over the mesh.
        await manager.wake_async(parent, by="workspace-sync")
        child = manager.stage_child(
            parent, assignment, warnings=warnings,
            created_by=actors.with_reason(actors.from_request(request), "workspace-sync"),
        )
        arranged = await _onboard_and_launch(request, child, assignment, parent=parent)
    except (spawn_mod.SpawnDenied, ManagerError, HarnessError, worktree_mod.WorktreeError,
            onboard.OnboardError, ValueError, TypeError, OSError) as exc:
        if child is not None:
            manager.discard(child.sdef.name)  # only drops an unstarted record
        return json_response({
            "error": (f"{error}\n" if error else "") + f"Could not start sync agent: {exc}",
            "operation_error": error, "agent_error": str(exc),
        }, status=409)
    return json_response({
        "status": "delegated", "agent": reposync_agent.summary(child), "plan": card,
        "operation_error": error, "warnings": warnings, "onboarding": arranged,
    }, status=202)


async def h_reposync_ws_sync(request: web.Request) -> web.Response:
    return await _ws_sync_serialized(request, _h_reposync_ws_sync)


async def _h_reposync_ws_sync(request: web.Request) -> web.Response:
    """Carry out the plan the Sync panel showed. The body names the plan's
    two tips; if either moved, nothing runs (409) and the fresh plan comes
    back. A plan that pushes goes through the approval gate first
    (:func:`_ws_authorize`); a fast-forward only moves the local branch and
    runs at once."""
    path, err = _ws_path(request)
    if err is not None:
        return err
    body = await _json_body(request)
    try:
        mode = _ws_agent_options(request, body)
    except (ValueError, ManagerError) as exc:
        return json_error(400, str(exc))
    try:
        p, card = await asyncio.to_thread(_ws_plan, path, request.match_info["name"], body)
    except reposync.SyncError as exc:
        return json_error(409, str(exc))
    active = _ws_active_agent(request, card, mode)
    if active is not None:
        return active
    if p.local != body.get("local") or p.upstream != body.get("upstream"):
        return json_response({"error": "the branch or the remote moved since the plan was shown",
                              "stale": True, "plan": card}, status=409)
    if p.action == reposync.UP_TO_DATE:
        return json_response({"ok": True, "plan": card, "new": p.local, "pushed": False})
    if mode == "agent":
        return await _ws_start_agent(request, card, body)
    approval = {"mode": "none"}
    if p.pushes:
        answer = _ws_authorize(request, card, str(body.get("request") or ""))
        if answer is not None:
            return answer
        approval = _ws_approval(request, str(body.get("request") or ""))

    def run() -> dict:
        # history_entry runs git (repo_identity), so it stays off the event loop
        entry = reposync_flow.history_entry(
            p.repo, kind="master", workspace=request.match_info["name"], remote=p.remote,
            branch=p.branch, by="web", approval=approval)
        return reposync_flow.recorded(entry, lambda: _ws_execute(p, card),
                                      reposync_flow.master_done(p))

    try:
        return json_response(await asyncio.to_thread(run))
    except reposync.SyncError as exc:
        if mode == "on_failure":
            return await _ws_start_agent(request, card, body, error=str(exc))
        return json_error(409, str(exc))


def _ws_approval(request: web.Request, rid: str) -> dict:
    """How a push :func:`_ws_authorize` let through was approved, for the
    history (claunch-iaihd): ``auto``, or the card a person answered."""
    if reposync_flow.approval_mode() == reposync_flow.AUTO:
        return {"mode": reposync_flow.AUTO}
    return reposync_flow.card_approval(request.app["reposync_gate"].get(rid) or {"id": rid})


def _ws_publish_card(path: str, name: str, body: dict, machine: str) -> dict:
    """The approval card for publishing a work branch. A force-with-lease is
    a different action than a plain push, so an approval of one does not
    cover the other (the action is one of the fields an approval binds)."""
    repo = reposync.toplevel(path)
    branch = str(body.get("branch") or "")
    remote = reposync.choose_remote(repo, str(body.get("remote") or "") or None)
    sha = reposync.git(repo, "rev-parse", "--verify", "-q", f"refs/heads/{branch}", check=False)
    if not sha:
        raise reposync.SyncError(f"no local branch {branch!r}")
    target = reposync.shared_ref(machine, branch)
    force = bool(body.get("force_with_lease"))
    return {
        "repo": repo, "workspace": name, "remote": remote,
        "remote_key": reposync.url_key(reposync.remotes(repo).get(remote, "")),
        "branch": target, "action": "publish-force" if force else "publish",
        "local": sha, "upstream": "", "source": branch,
        "summary": f"push {branch} {sha[:8]} to {remote}/{target}"
                   + (" (force-with-lease)" if force else ""),
    }


async def h_reposync_ws_branch_push(request: web.Request) -> web.Response:
    return await _ws_sync_serialized(request, _h_reposync_ws_branch_push)


async def _h_reposync_ws_branch_push(request: web.Request) -> web.Response:
    """Publish a local work branch as ``<this machine>/<branch>``, behind the
    approval gate like every push (:func:`_ws_authorize`)."""
    path, err = _ws_path(request)
    if err is not None:
        return err
    body = await _json_body(request)
    try:
        mode = _ws_agent_options(request, body)
    except (ValueError, ManagerError) as exc:
        return json_error(400, str(exc))
    if not str(body.get("branch") or ""):
        return json_error(400, "branch is required")
    machine = _machine_name(request)
    try:
        card = await asyncio.to_thread(_ws_publish_card, path, request.match_info["name"], body, machine)
    except reposync.SyncError as exc:
        return json_error(409, str(exc))
    active = _ws_active_agent(request, card, mode)
    if active is not None:
        return active
    if mode == "agent":
        return await _ws_start_agent(request, card, body)
    answer = _ws_authorize(request, card, str(body.get("request") or ""))
    if answer is not None:
        return answer

    def push() -> dict:
        # the sha the approval named is what goes out, never a later tip
        current = reposync.git(card["repo"], "rev-parse", "--verify", "-q",
                               f"refs/heads/{card['source']}", check=False)
        if current != card["local"]:
            raise reposync.SyncError(f"{card['source']} moved after the approval; nothing was pushed")
        ref = reposync.publish_branch(card["repo"], card["remote"], card["source"], machine,
                                      force_with_lease=card["action"] == "publish-force")
        return {"ok": True, "remote": card["remote"], "ref": ref, "sha": card["local"]}

    approval = _ws_approval(request, str(body.get("request") or ""))

    def run() -> dict:
        # history_entry runs git (repo_identity), so it stays off the event loop
        entry = reposync_flow.history_entry(
            card["repo"], kind="publish", workspace=request.match_info["name"],
            remote=card["remote"], branch=card["source"], by="web", approval=approval,
            ref=card["branch"], force=card["action"] == "publish-force")
        return reposync_flow.recorded(entry, push,
                                      lambda out: {"after": out["sha"], "pushed": True})

    try:
        return json_response(await asyncio.to_thread(run))
    except reposync.SyncError as exc:
        if mode == "on_failure":
            return await _ws_start_agent(request, card, body, error=str(exc))
        return json_error(409, str(exc))


async def h_reposync_ws_branch_pull(request: web.Request) -> web.Response:
    """Fetch another machine's ``<machine>/<branch>`` into the local branch
    of the same name, fast-forward only."""
    path, err = _ws_path(request)
    if err is not None:
        return err
    body = await _json_body(request)
    machine, branch = str(body.get("machine") or ""), str(body.get("branch") or "")
    if not machine or not branch:
        return json_error(400, "machine and branch are required")

    def run() -> dict:
        repo = reposync.toplevel(path)
        remote = reposync.choose_remote(repo, str(body.get("remote") or "") or None)
        entry = reposync_flow.history_entry(
            repo, kind="pull", workspace=request.match_info["name"], remote=remote,
            branch=branch, by="web", approval={"mode": "none"},
            ref=reposync.shared_ref(machine, branch))
        local, sha = reposync_flow.recorded(
            entry, lambda: reposync.receive_branch(repo, remote, machine, branch),
            lambda got: {"after": got[1], "pushed": False})
        return {"ok": True, "remote": remote, "branch": local, "sha": sha}

    try:
        return json_response(await asyncio.to_thread(run))
    except reposync.SyncError as exc:
        return json_error(409, str(exc))


async def h_reposync_request_submit(request: web.Request) -> web.Response:
    """A session's sync plan, waiting for a person. The CLI decided that the
    caller is a managed session and that the setting asks for approval; the
    daemon only holds the question."""
    body = await _json_body(request)
    try:
        record = request.app["reposync_gate"].submit(
            session=str(body.get("session") or ""), plan=body.get("plan") or {},
        )
    except ValueError as exc:
        return json_error(400, str(exc))
    return json_response({"ok": True, "request": record})


async def h_reposync_requests_list(request: web.Request) -> web.Response:
    return json_response({"requests": request.app["reposync_gate"].list()})


async def h_reposync_request_get(request: web.Request) -> web.Response:
    record = request.app["reposync_gate"].get(request.match_info["rid"])
    if record is None:
        return json_error(404, "no reposync request with that id")
    return json_response({"request": record})


async def h_reposync_request_approve(request: web.Request) -> web.Response:
    record = request.app["reposync_gate"].approve(request.match_info["rid"], decided_by="web")
    if record is None:
        return json_error(409, "no pending reposync request with that id")
    return json_response({"ok": True, "request": record})


async def h_reposync_request_reject(request: web.Request) -> web.Response:
    record = request.app["reposync_gate"].reject(request.match_info["rid"], decided_by="web")
    if record is None:
        return json_error(409, "no pending reposync request with that id")
    return json_response({"ok": True, "request": record})


async def h_relay_meshes(request: web.Request) -> web.Response:
    """Meshes this daemon could attach: public ones owned by any daemon on
    any connected relay, plus offers pushed here (one hop, deduplicated)."""
    mm = _mesh_mgr(request)
    result = await mm.discover()
    result["offers"] = mm.offers_received()
    return json_response(result)


async def h_mesh_attach(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = await _mesh_mgr(request).attach(
        request.match_info["mesh"], code=str(body.get("code") or "") or None,
        project=str(body.get("project") or ""),
    )
    return json_response(result, status=202 if result.get("pending") else 201)


async def h_mesh_detach(request: web.Request) -> web.Response:
    force = request.query.get("force", "") in ("1", "true", "yes")
    result = await _mesh_mgr(request).detach(
        request.match_info["mesh"], force=force
    )
    return json_response(result)


async def h_mesh_project(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = _mesh_mgr(request).set_project(
        request.match_info["mesh"], str(body.get("project") or "")
    )
    return json_response(result)


async def h_mesh_visibility(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = await _mesh_mgr(request).set_visibility(
        request.match_info["mesh"], str(body.get("visibility") or "")
    )
    return json_response(result)


async def h_mesh_offer(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = await _mesh_mgr(request).offer_mesh(
        request.match_info["mesh"], str(body.get("machine") or "")
    )
    return json_response(result, status=201)


async def h_mesh_offer_cancel(request: web.Request) -> web.Response:
    result = await _mesh_mgr(request).cancel_offer(
        request.match_info["mesh"], request.match_info["machine"]
    )
    return json_response(result)


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
        by=str(body.get("by") or ""),
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
    """Answer a wire request with no. ``actor`` gates it like a link edit;
    ``by`` only names the decider (the CLI's calling session)."""
    body = await _json_body(request)
    result = _mesh_mgr(request).decline_wire_request(
        request.match_info["mesh"],
        str(body.get("a") or ""),
        str(body.get("b") or ""),
        actor=str(body.get("actor") or ""),
        reason=str(body.get("reason") or ""),
        by=str(body.get("by") or ""),
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
        offer=str(body.get("offer") or ""),
    )
    return json_response(result)


async def h_peer_meshes(request: web.Request) -> web.Response:
    return json_response({"meshes": _mesh_mgr(request).peer_meshes_list()})


async def h_peer_mesh_offer(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = await _mesh_mgr(request).peer_offer_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        cancel=bool(body.get("cancel")),
        project=str(body.get("project") or ""),
        members=body.get("members") or 0,
    )
    return json_response(result)


async def h_peer_mesh_offer_check(request: web.Request) -> web.Response:
    """The owner's half of an offer: the daemon it was pushed to asks back."""
    body = await _json_body(request)
    return json_response(_mesh_mgr(request).peer_offer_check(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
    ))


async def h_peer_renamed(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = _mesh_mgr(request).peer_renamed_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
        str(body.get("old") or ""),
    )
    return json_response(result)


async def h_relay_peer_rename(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = _mesh_mgr(request).rename_peer(
        request.match_info["machine"], str(body.get("new") or "")
    )
    return json_response(result)


async def h_peer_detach(request: web.Request) -> web.Response:
    body = await _json_body(request)
    result = _mesh_mgr(request).peer_detach_accept(
        str(body.get("mesh") or ""),
        str(body.get("machine") or ""),
        str(body.get("token") or ""),
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


def _peer_shadow_member(request: web.Request, body: dict):
    """The (session, member) a ``/peer/shadow/*`` call names, or the refusal.

    Token and membership are checked by the mesh (``peer_shadow_member``);
    a refusal is 403 here, where peer ops say 400, because nothing about
    the request is malformed -- the caller is not allowed to see it.
    """
    mm = _mesh_mgr(request)
    session_name = str(body.get("session") or "")
    try:
        member = mm.peer_shadow_member(
            str(body.get("mesh") or ""),
            str(body.get("machine") or ""),
            str(body.get("token") or ""),
            session_name,
        )
    except MeshError as exc:
        raise web.HTTPForbidden(
            text=json.dumps({"error": str(exc)}), content_type="application/json"
        ) from None
    manager: SessionManager = request.app["manager"]
    try:
        session = manager.get(session_name)
    except ManagerError:
        raise web.HTTPNotFound(
            text=json.dumps({"error": f"session {session_name!r} is gone"}),
            content_type="application/json",
        ) from None
    return session, member


async def h_peer_shadow_cards(request: web.Request) -> web.Response:
    body = await _json_body(request)
    mm = _mesh_mgr(request)
    try:
        members = mm.peer_shadow_members(
            str(body.get("mesh") or ""),
            str(body.get("machine") or ""),
            str(body.get("token") or ""),
        )
    except MeshError as exc:
        return json_error(403, str(exc))
    manager: SessionManager = request.app["manager"]

    def build() -> list:
        cards = []
        # One read of the status-check file for every card, as the rail's
        # session poll does it.
        checks = status_checks.digests([m.session for m in members])
        for member in members:
            try:
                session = manager.get(member.session)
            except ManagerError:
                continue
            cwd = _session_cwd(session)
            flow = (
                _cflow_entry(manager, cwd, session.sdef.name, reports=False, slim=True)
                if cwd else None
            )
            cards.append(shadow_mod.card(
                session, member, cflow=flow, checks=checks.get(member.session),
            ))
        return cards

    return json_response({"cards": await asyncio.to_thread(build)})


async def h_peer_shadow_stream(request: web.Request) -> web.StreamResponse:
    body = await _json_body(request)
    session, _member = _peer_shadow_member(request, body)
    return await shadow_mod.serve_stream(request, session, request.app)


async def h_peer_shadow_line(request: web.Request) -> web.Response:
    body = await _json_body(request)
    session, _member = _peer_shadow_member(request, body)
    try:
        cfg = store.daemon_config()
    except store.StoreError:
        cfg = None
    if not shadow_mod.input_enabled(cfg):
        return json_error(
            403, "this daemon does not take the session line from peers "
                 "(daemon.shadow_input is off)")
    machine = str(body.get("machine") or "")
    try:
        result = await shadow_mod.type_line(
            session, body.get("text"), body.get("input_id"),
            origin=f"peer:{machine}",
        )
    except shadow_mod.ShadowRefused as exc:
        return json_error(exc.status, str(exc))
    return json_response(result)


async def h_peer_shadow_transcript(request: web.Request) -> web.Response:
    """One page of a member's conversation, for a linked peer's reader.

    The page the local transcript route serves (h_session_transcript), cut
    to what leaves this daemon (``shadow.transcript_cut``). In a thread for
    the same reason: the first page of a large transcript builds its index.
    """
    body = await _json_body(request)
    session, _member = _peer_shadow_member(request, body)
    try:
        cfg = store.daemon_config()
    except store.StoreError:
        cfg = None
    if not shadow_mod.transcript_enabled(cfg):
        return json_error(
            403, "this daemon does not show its sessions' transcripts to peers "
                 "(daemon.shadow_transcript is off)")
    try:
        before, limit = shadow_mod.page_args(body.get("before"), body.get("limit"))
    except shadow_mod.ShadowRefused as exc:
        return json_error(exc.status, str(exc))
    page = await asyncio.to_thread(shadow_mod.transcript_page, session, before, limit)
    return json_response(page)


async def h_shadows_list(request: web.Request) -> web.Response:
    """Every other daemon's member of every mesh held here, with its card."""
    holder = request.app["shadow"]
    mm = _mesh_mgr(request)
    directory = holder.get("directory")
    if directory is None or holder.get("mesh") is not mm:
        directory = holder["directory"] = shadow_mod.Directory(mm)
        holder["mesh"] = mm
    return json_response({
        "machine": mm.machine or None,
        "relay_connected": bool(mm.relay_connected()),
        "shadows": await directory.list(),
    })


async def h_shadow_ws(request: web.Request) -> web.WebSocketResponse:
    """A shadow's terminal for this page: output only, relayed from its host."""
    machine = request.match_info["machine"]
    session = request.match_info["session"]
    ws = web.WebSocketResponse(heartbeat=ws_mod.HEARTBEAT)
    await ws.prepare(request)
    request.app["websockets"].add(ws)
    conns = connections.install(request.app)
    record = conns.opened("shadow", f"{machine}/{session}", request, ws=ws)
    try:
        await shadow_mod.bridge_to_viewer(ws, _mesh_mgr(request), machine, session)
    finally:
        request.app["websockets"].discard(ws)
        if not ws.closed:
            await ws.close()
        conns.closed(record, ws.close_code, ws.exception())
    return ws


async def h_shadow_line(request: web.Request) -> web.Response:
    """The session line, typed into another daemon's member via its host."""
    body = await _json_body(request)
    text = body.get("text")
    input_id = body.get("input_id")
    if not isinstance(text, str) or not text.strip():
        return json_error(400, "'text' must be a non-empty string")
    if not isinstance(input_id, str) or not input_id.strip():
        return json_error(400, "'input_id' must be a non-empty string")
    try:
        result = await _mesh_mgr(request).shadow_call(
            request.match_info["machine"], request.match_info["session"],
            "/peer/shadow/line", {"text": text, "input_id": input_id},
        )
    except mesh_mod.PeerUnreachable as exc:
        return json_error(502, str(exc))
    return json_response(result)


async def h_shadow_transcript(request: web.Request) -> web.Response:
    """A page of another daemon's member's conversation, via its host.

    ``before``/``limit`` as the local route takes them. The host's answer is
    cut again here (``shadow.transcript_cut``), as a card is. A host that
    answers the route with a plain 404 predates it: said so, rather than
    passed on as a bare status the page cannot explain.
    """
    machine = request.match_info["machine"]
    try:
        before, limit = shadow_mod.page_args(
            request.query.get("before"), request.query.get("limit"))
    except shadow_mod.ShadowRefused as exc:
        return json_error(exc.status, str(exc))
    body: dict = {"limit": limit}
    if before is not None:
        body["before"] = before
    try:
        result = await _mesh_mgr(request).shadow_call(
            machine, request.match_info["session"], "/peer/shadow/transcript", body,
        )
    except mesh_mod.PeerUnreachable as exc:
        if exc.status == 404:
            return json_error(
                502, f"daemon {machine!r} serves no transcripts to peers — "
                     f"it runs a claunch older than the shadow transcript")
        return json_error(502, str(exc))
    available = isinstance(result, dict) and result.get("available") is True
    return json_response(shadow_mod.transcript_cut(result, available=available))


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


@shared_reads.shared("sessions")
async def h_sessions_list(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    rail_view = request.query.get("view") == "rail"
    list_state = request.query.get("state") or "all"
    if list_state not in {
        "all", "active", "current", "killed", "paused", "hold", "delayed",
        "failed", "archived",
    }:
        return json_error(400, f"invalid session list state: {list_state!r}")
    # ``?project=`` narrows the list to one project's sessions; absent means
    # every project, which is what this route has always answered. Applied
    # here, before the per-session assembly, for the same reason the state
    # filter is: what the reader cannot see is not built.
    project_filter = str(request.query.get("project") or "").strip()
    # ``?kind=`` keeps the sessions that declared that kind at creation
    # (SessionDef.kind); absent means every session, kind or not.
    kind_filter = str(request.query.get("kind") or "").strip()
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
        if list_state == "hold" and category != session_mod.CATEGORY_HOLD:
            continue
        if list_state == "delayed" and category != session_mod.CATEGORY_DELAYED:
            continue
        if list_state == "failed" and category != session_mod.CATEGORY_FAILED:
            continue
        if list_state == "archived" and not archived:
            continue
        if not projects.matches(s.sdef.project, project_filter):
            continue
        if kind_filter and getattr(s.sdef, "kind", None) != kind_filter:
            continue
        sessions.append(s)
    winddowns = request.app["beads"].winddowns
    handoffs = request.app["handoff"].pending

    def collect() -> tuple:
        # The per-session assembly, in a worker: ``attach`` re-reads a
        # transcript tail whenever its file grew, and ten busy sessions grow
        # theirs continuously, so this loop was 30-150ms of the event loop
        # per poll -- time no terminal socket could be served in.
        # One inline read budget for the whole list: after a daemon restart
        # every transcript is unread, and whatever this request cannot read
        # within it is finished in the background and shown as partial. The
        # background work is handed over after the loop (``release``), so it
        # does not compete with the loop for the GIL.
        usage_budget = tokenusage.budget()
        try:
            rows = _collect(usage_budget)
        finally:
            usage_budget.release()
        # The status-check answers for every row, read here rather than on
        # the loop: the file read and the cleaning of every session's reports
        # held the GIL on the thread that pumps every terminal
        # (claunch-2t37a).
        try:
            checks = status_checks.digests([info.get("name") or "" for info in rows])
        except status_checks.StatusCheckError:
            checks = {}
        return rows, checks

    def _collect(usage_budget) -> list:
        out = []
        # One look at the metering files for the whole list, however many
        # rows read it (claunch-vyw9s), and one read of its age bound
        # (claunch-2t37a).
        meter = metering.snapshot()
        # And one listing of the live files: whether a call is in flight now.
        meter_live = metering.live_index()
        # What the daemon's restart commands left running for each session
        # (claunch-ihwel): the tracker's memory, kept current by its own
        # clock, so a list poll reads no process table.
        try:
            running = session_procs.tracker().summary()
        except Exception:
            running = {}
        try:
            meter_age = metering.summary_max_age()
        except store.StoreError:
            # Each row reads it for itself, as before, and answers as before.
            meter_age = None
        for s in sessions:
            # An archived record carries none of the per-row reads below
            # (context, tool calls, ``tps``, ``token_usage``): the list holds
            # hundreds of them, and each read is work in this worker that
            # holds the GIL while the event loop -- every terminal and
            # control socket -- waits (claunch-vyw9s). Nobody is watching a
            # retired session's numbers move; its detail panel still reads
            # them all when opened (``h_session_meta``).
            archived = session_mod.session_category(s) == session_mod.CATEGORY_ARCHIVED
            # An exited record's transcript no longer grows: its readings
            # (and its PRs) are read once after the exit and kept while the
            # files they came from are unchanged (claunch-0gysk). Only
            # ``tps`` is read here, off the shared snapshot.
            kept = s.exited and not archived
            if archived:
                info = s.info()
            elif kept:
                info = exited_rows.attach(s, usage_budget, _session_cwd(s))
                metering.attach(info, meter, meter_age, meter_live)
            else:
                info = ctxsize.attach(s)
                # The session's latest throughput through the metering shim
                # (``tps``), read off the record file tails; absent when the
                # session never went through a shim (the OAuth routes).
                metering.attach(info, meter, meter_age, meter_live)
                # What the conversation has spent so far (``token_usage``),
                # read incrementally off the same transcript; absent where the
                # harness has no reader.
                tokenusage.attach(info, s.sdef, usage_budget)
                # Where the session works when that is not where it was
                # started (``work_cwd``): read off the same transcript, so
                # gates asking "where does this session stand" measure the
                # worktree it moved into rather than the main checkout
                # (claunch-4zc51.1.2).
                work_checkout.attach(info, s.sdef)
            # The cached briefing's one-liner, when it exists — rides the list
            # the UI already polls so a row can show it without an open card or
            # an LLM call, and so a browser refresh repaints it from the
            # daemon's session state instead of regenerating.
            d = briefing.digest(info.get("name") or "")
            if d:
                info["briefing"] = d
            # Same scoped PR records as the detail panel, read locally and
            # cached; a list poll never starts a GitHub request.
            if not kept:
                info["prs"] = session_prs.for_session(_session_cwd(s), info["name"])
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
            if running.get(info.get("name") or ""):
                info["procs"] = running[info["name"]]
            if reminder_service is not None:
                info["session_reminder"] = reminder_service.status(
                    info.get("name") or "", cfg=reminder_cfg, session=s,
                )
            out.append(info)
        return out

    attached, check_digests = await asyncio.to_thread(collect)
    for s, info in zip(sessions, attached):
        # Which git branch the session's checkout is on — one fact that tells
        # two sessions in the same worktree apart without opening either. On
        # the loop, because a miss schedules its git read on the loop (see
        # _read_branch_later); the hit itself is one stat and a dict lookup.
        info["branch"] = _branch_of(info.get(work_checkout.FIELD) or _session_cwd(s))
        # The rail's observe-pin box and the observer card's read the same
        # definition field. Answering it here rather than in the observer's
        # snapshot is what lets the rail draw it off the poll it already
        # makes, with no second request and no state of its own to keep.
        info["observe_pin"] = bool(getattr(s.sdef, "observe_pin", False))
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
    published = manager.progress.snapshot(info.get("name") or "" for info in attached)
    # The boot's restore queue (claunch-v4uyt): each delayed row says where it
    # stands, and the list says how far the queue has got, so the rail can
    # tell "starts in a minute" apart from a hold.
    queue = request.app.get("restore_queue")
    queue_status = queue.status() if queue is not None else None
    queue_places = queue.positions() if queue_status else {}
    for info in attached:
        place = queue_places.get(info.get("name") or "")
        if place:
            info["restore_queue"] = place
        checks = check_digests.get(info.get("name") or "")
        if checks:
            info["status_checks"] = checks
        # What the session last said about its own progress; absent when it
        # never published any (daemon/progress.py).
        prog = published.get(info.get("name") or "")
        if prog:
            info["progress"] = prog
    if rail_view:
        # The dashboard reads this resource repeatedly. The detail panel has
        # its own /meta request, so an opening task and environment do not
        # belong in every rail response.
        # ``delayed_at``/``failed_at``/``restore_queue`` are the two partitions
        # the rail draws apart from hold and killed (claunch-0j6gb): without
        # them every delayed row reached the page as a plain hold, and a
        # boot's queue read as sessions that failed to come back
        # (claunch-v4uyt).
        rail_fields = {
            "name", "harness", "profile", "cwd", "args", "model", "effort",
            "tools", "restore", "conversation_id", "role", "parent", "borrow",
            "null_token", "issue", "keep_alive", "reminder_paused", "note",
            "reminder_note",
            "status",
            "score_goal", "user_reward", "user_penalty",
            "pid", "exit_code", "created_at", "last_output_at",
            "last_visited_at", "last_input_at", "last_activity_at", "viewers",
            "moved_rows", "tool_calls",
            "exited_at", "archived_at", "paused_at", "held_at", "delivery_hold",
            "delayed_at", "failed_at", "restore_queue",
            "pending_deliveries", "compacting",
            "context", "branch", "briefing", "winddown", "session_reminder",
            # Whose conversation this session is a copy of, and the merge or
            # handoff the operator has already asked for. The header's
            # fork/merge pair reads both off this list and nothing else
            # (``forkControlState``/``handoffControlState`` in app.js), so
            # leaving them out is not a saving: it hid ``↩ merge`` on every
            # quick-fork there has ever been, and left ``⑂ fork`` offered on
            # the copy — the fork button's guard is the same absent field.
            "quick_fork_of", "handoff", "self_merge_allowed",
            "status_checks", "tps",
            # The project a session is filed under. The page polls every
            # project and narrows the rail itself (``railSessions`` in
            # app.js), so a row without this key reads as the default
            # project's: with it left out, picking any other project drew an
            # empty rail while "All projects" still listed them.
            "project",
            # What the session declared itself to be at creation, and the
            # progress it publishes while it runs (daemon/progress.py): the
            # rail's meta line and progress line read both off this poll.
            "kind", "progress", "prs",
        }
        attached = [
            {key: value for key, value in info.items() if key in rail_fields}
            for info in attached
        ]
    body = {
        "sessions": attached, "llm_configured": llm_ok, "rag_configured": rag_ok,
    }
    if queue_status:
        body["restore_queue"] = queue_status
    return json_response(body)


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
    status, payload = await _create_session(request, await _json_body(request))
    return json_response(payload, status=status)


async def _create_session(request: web.Request, body: dict) -> Tuple[int, dict]:
    """The body of :func:`h_sessions_create`, as (status, payload), so the
    operator's start button creates its session through the same checks."""
    manager: SessionManager = request.app["manager"]
    # The project is settled first: it is checked against the registry (a
    # session filed under an unknown name is reachable from no listing), and
    # its default workspace is what a request naming no directory starts
    # in — before the worktree below is resolved against that directory.
    project_name = str(body.get("project") or "").strip()
    if project_name:
        try:
            project_name = projects.require(project_name).name
        except projects.ProjectError as exc:
            return 400, {"error": str(exc)}
        body["project"] = project_name
    if not str(body.get("cwd") or "").strip():
        default_cwd = projects.default_cwd(project_name)
        if default_cwd:
            body["cwd"] = default_cwd
    # The browser sends a worktree name separately from the session directory.
    # Resolve it before SessionDef is built so the persistent definition keeps
    # the actual checkout path, just like the CLI launch path does. An empty
    # name means the standard generated name; omitting the key means no
    # worktree.
    if "worktree" in body:
        choice = body.pop("worktree")
        if choice is True or choice is None:
            choice = ""
        if not str(choice).strip():
            # The generated name is the session's, so the session's name is
            # settled first, as a spawn does for its child (claunch-2yiij.3):
            # the default would read the pane or session from this daemon's
            # own environment, which is nobody's. A taken name is refused
            # here, with the status stage() below would give it, before a
            # checkout is cut.
            try:
                body["name"] = manager.settle_name(str(body.get("name") or ""))
            except ManagerError as exc:
                return 409 if "already exists" in str(exc) else 400, {"error": str(exc)}
            choice = worktree_mod.default_name(token=body["name"])
        try:
            base = cflow_state.resolve_cwd(body.get("cwd") or None)
            tree = worktree_mod.resolve(
                base, str(choice),
                rebase_onto=str(body.pop("rebase_onto", "") or ""),
                branch_from=str(body.pop("branch_from", "") or ""),
            )
        except worktree_mod.WorktreeError as exc:
            return 400, {"error": str(exc)}
        if tree is None:
            return 400, {"error": "worktree selection did not produce a checkout"}
        body["cwd"] = str(tree.path)
    if "harness" in body:
        return 400, {
            "error": "harness is read-only and comes from profile; omit 'harness'"
        }
    if not str(body.get("profile") or "").strip():
        return 400, {"error": "a session needs profile; its harness comes from it"}
    # What the session is for (SessionDef.kind), checked here because the
    # definition's own reader only trims: a value it would store verbatim
    # must already be one the list column and the ``?kind=`` filter can take.
    if "kind" in body:
        try:
            body["kind"] = check_kind(body.get("kind"))
        except ValueError as exc:
            return 400, {"error": str(exc)}
    # The prompt packs the form's checkboxes left on: only their shape is
    # checked here. They are read from the workspace (together with the ones
    # the session's roles require) in _onboard_and_launch, which spawns share.
    try:
        body["prompt_packs"] = _asked_prompt_packs(body)
    except prompt_packs.PackError as exc:
        return 400, {"error": str(exc)}
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
    definition.pop("prompt_packs", None)
    # v2 start inputs belong to the run (chart.json), not to the session.
    definition.pop("cflow_inputs", None)
    # Who created it is the request's, never the body's (claunch-o6xws).
    definition["created_by"] = actors.from_request(request)
    try:
        sdef = SessionDef.from_dict(definition)
    except (KeyError, ValueError, TypeError) as exc:
        return 400, {"error": f"bad session definition: {exc}"}
    try:
        session = manager.stage(sdef)
    except ManagerError as exc:
        return 409 if "already exists" in str(exc) else 400, {"error": str(exc)}
    try:
        result = await _onboard_and_launch(request, session, body)
    except onboard.OnboardError as exc:
        return 400, onboard.error_body(exc)
    return 201, {**session.info(), **result}


def _asked_prompt_packs(body: dict) -> List[str]:
    """``body["prompt_packs"]`` as a list of pack ids (a comma string too)."""
    raw = body.get("prompt_packs")
    if raw in (None, "", []):
        return []
    if isinstance(raw, str):
        raw = [part.strip() for part in raw.split(",") if part.strip()]
    if not isinstance(raw, list):
        raise prompt_packs.PackError("prompt_packs must be a list of pack ids")
    return [prompt_packs.check_id(str(x)) for x in raw]


def _session_prompt_packs(
    request: web.Request, body: dict, plan, *, name: str, cwd: str
):
    """The packs a session is given: ticked ones plus role-required ones.

    Roles are matched by their canonical names on the mesh being joined, so
    an alias the roleset knows brings the same packs as the role itself.
    Read from the workspace that owns ``cwd``; see :func:`prompt_packs.choose`.
    """
    roles = [r for r in (plan.role, *plan.subroles) if r]
    roleset = _plan_roleset(request, plan)
    if roleset is not None:
        roles = [_plan_primary_role(roleset, plan, name),
                 *(roleset.canonical(r) or r for r in plan.subroles)]
    asked = _asked_prompt_packs(body)
    required = prompt_packs.required_for(roles)
    # The daemon's own prompt comes first, for every role, unless the
    # request turned it off (daemon_prompt.wanted).
    own = daemon_prompt.pack() if daemon_prompt.wanted(body) else None
    head = prompt_packs.records([own]) if own is not None else []
    if not asked and not required:
        return head, {}
    owner = workspaces.owning(str(cflow_state.resolve_cwd(cwd or None)))
    recs, required = prompt_packs.choose(
        Path(owner.path) if owner is not None else None, asked, roles
    )
    return head + recs, required


def _plan_roleset(request: web.Request, plan):
    """The roleset of the local mesh ``plan`` joins, or None."""
    mm = _mesh_mgr(request)
    if not plan.mesh or "@" in plan.mesh or mm is None:
        return None
    try:
        return mm.get(plan.mesh).roleset
    except MeshError:
        return None


def _plan_primary_role(roleset, plan, name: str) -> str:
    """The primary role as the join will store it: an explicit one through
    the aliases, else inferred from the handle -- so a member named
    ``unreal1`` is treated as ``--role unreal`` would be."""
    if roleset is None:
        return plan.role or ""
    try:
        return roleset.resolve(plan.handle or name, plan.role) or ""
    except mesh_roles.RoleError:
        return plan.role or ""


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
        try:
            selected = score_goal.enabled(body.get(
                "score_goal", store.daemon_config().get("score_goal_default", False)
            ))
        except ValueError as exc:
            raise onboard.OnboardError(str(exc)) from None
        session.sdef = replace(session.sdef, score_goal=selected)
        # The operator's note, written in the form that starts the session
        # (claunch-zy9yk): the same field and limit as the note endpoint
        # (manager.set_note), set here so create and spawn both carry it.
        if "note" in body:
            raw_note = body.get("note")
            if raw_note is not None and not isinstance(raw_note, str):
                raise onboard.OnboardError("'note' must be text")
            note_text = str(raw_note or "").strip()
            if len(note_text) > MAX_NOTE:
                raise onboard.OnboardError(f"a note is at most {MAX_NOTE} characters")
            session.sdef = replace(session.sdef, note=note_text or None)
        # Inside the discarding try, and before anything is arranged: a board
        # answer that contradicts itself is checkable without a session, and
        # the alternative is a request that half-happens.
        try:
            beads_mod.check_request(body)
        except beads_mod.BoardRequestError as exc:
            raise onboard.OnboardError(str(exc)) from None
        image_ids = _opening_image_ids(body, session.sdef.harness)
        # The operator's explicit "no workflow" on a session they create
        # (claunch-val3l.1): the CLI's --no-workflow, the form's cleared
        # role default. Taken out before preflight, which reads '-' as a
        # workflow name, and recorded on the session below. A spawn's '-'
        # keeps its own meaning (decline the parent's pair, inherit_workflow).
        declined_at_creation = (
            not parent and body.get("workflow") == onboard.NO_WORKFLOW
        )
        if declined_at_creation:
            body.pop("workflow")
        # The v2 layers compiled off the loop (claunch-cb65c.1): what follows
        # reads them synchronously, and a cold cache (a new cwd, a daemon
        # just started) cost seconds of compile on the loop.
        parent_cwd = manager.get(parent).sdef.cwd if parent else ""
        await asyncio.to_thread(cflow2_web.warm, cwd)
        if parent:
            await onboard.inherit_mesh(
                body,
                parent=parent,
                mesh_mgr=_mesh_mgr(request),
                project=session.sdef.project or "",
            )
            onboard.inherit_workflow(
                body,
                parent=parent,
                parent_cwd=manager.get(parent).sdef.cwd,
                cwd=cwd,
            )
        if body.get("workflow"):
            await asyncio.to_thread(
                cflow2_web.warm, cwd, str(body.get("workflow") or "").strip(),
                body.get("workflow_version"), (parent_cwd,),
            )
        plan = onboard.preflight(
            body,
            mesh_mgr=_mesh_mgr(request),
            session_name=name,
            cwd=cwd,
            harness=session.sdef.harness,
            parent=parent or "",
            parent_cwd=manager.get(parent).sdef.cwd if parent else "",
            profile=session.sdef.profile or "",
        )
        # Checked before anything is arranged, like the board answer above: a
        # pack, ticked or role-required, that exists neither where the session
        # will run nor in claunch is a refused request, never a session that
        # started without guidance it was promised. One the workspace has not
        # installed goes in as claunch's copy (prompt_packs.pack_copy).
        try:
            packs, packs_required = _session_prompt_packs(
                request, body, plan, name=name, cwd=cwd
            )
            if body.get("prompter_prompts") is not None:
                raise prompt_packs.PackError("Prompter prompts belong in Opening task; reopen the form")
            packs += await prompter.selected_records(body.get("prompter_contexts"))
        except prompt_packs.PackError as exc:
            raise onboard.OnboardError(str(exc)) from None
        if packs:
            session.sdef = replace(session.sdef, prompt_packs=tuple(packs))
        # TEMPORARY (claunch-z7b0n): a v2 start input 'issue' is folded into
        # the board answer, or refused when it contradicts it. Removed with
        # the overwrite below once no v2 workflow declares that input.
        if plan.workflow_version == 2 and plan.cflow_inputs:
            try:
                beads_mod.adopt_input_issue(body, plan.cflow_inputs)
            except beads_mod.BoardRequestError as exc:
                raise onboard.OnboardError(str(exc)) from None
    except Exception:
        manager.discard(name)
        raise
    manager.assign_identity(session, plan.identity)

    # The board link, settled before the opening is composed so the agent's
    # first message names the issue it will be working — the record the
    # workflows tell it to read (`claunch beads show <id> --json`). Never a
    # reason to refuse the session: a board that cannot be written is logged
    # and the session starts without one.
    #
    # The one exception (claunch-z7b0n, W1): a request that names an issue the
    # board was read and does not hold is refused like any other bad request
    # -- whichever field the id came in (the Board pick, --issue, spawn's
    # issue, a v2 input folded in above). An `issue: <id>` read out of the
    # task or context is not refused: prose like "Known issue: flaky tests"
    # matches the same pattern (s88 P1). A board that cannot be read is
    # still no reason to refuse; that is reported in `unsettled` below.
    unsettled: dict = {}
    try:
        linked = await request.app["beads"].ensure_issue(
            session, body=body, parent=parent, manager=manager, unsettled=unsettled
        )
    except beads_mod.IssueNotOnBoard as exc:
        manager.discard(name)
        raise onboard.OnboardError(str(exc)) from None
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
        # what was asked. ISSUE_PENDING likewise: it tells the session that an
        # issue is coming and how to record it (claunch-5p2j4).
        none = beads_mod.none_mode(body)
        if unsettled:
            # The request named an issue and the board could not be asked:
            # the session starts without it, and both the caller and the
            # session are told which id and why, so the next step is theirs.
            report["beads"] = {"issue": None, "input_issue": unsettled["issue"],
                               "settled": False, "why": unsettled["why"]}
            plan = replace(
                plan,
                task=(plan.task + "\n\n" if plan.task else "")
                + f"[board] Issue {unsettled['issue']} was named for this "
                  f"session but is not linked: {unsettled['why']}. Link it "
                  "once the board is reachable, or ask whoever started you.",
            )
        elif none and (plan.wanted
                       or none in (beads_mod.NONE_AUTO, beads_mod.ISSUE_PENDING)):
            report["beads"] = {"issue": None, "mode": none}
            plan = replace(
                plan,
                task=(plan.task + "\n\n" if plan.task else "")
                + beads_mod.compose_none_note(none, session=name),
            )

    # A session someone created by hand, holding a mesh role, with no run
    # (claunch-val3l.1). Nothing is started for it here: a run begins only on
    # its user's explicit yes, so the opening carries the question (when its
    # role selects a workflow) or, when the operator already said no, the
    # recorded refusal that a mesh leader reads off the roster.
    if not parent and plan.mesh and not plan.workflow:
        offered, version = "", 0
        # the role the join stores, handle-inferred or through an alias
        role = _plan_primary_role(_plan_roleset(request, plan), plan, name)
        if role:
            try:
                entries = await asyncio.to_thread(_startable_all, cwd)
                offered, version = onboard.role_default_workflow(
                    entries, role, _role_workflows_or_none()
                )
            except Exception:  # noqa: BLE001 - a hint, never a refusal
                log.warning("could not resolve the %s role's workflow for %s",
                            role, name, exc_info=True)
        note = ""
        # A '-' with nothing to refuse (the form judged another directory's
        # list, or the override picks none) records nothing.
        if declined_at_creation and offered:
            session.sdef = replace(session.sdef, cflow_declined={
                "workflow": offered,
                "reason": "declined by the operator at creation",
                "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "by": "operator",
            })
            note = onboard.compose_cflow_declined_note(workflow=offered)
            report["cflow"] = {"declined": True, "workflow": offered}
        elif offered:
            note = onboard.compose_cflow_offer(
                role=role, workflow=offered, version=version
            )
            report["cflow"] = {"offered": offered}
        if note:
            plan = replace(
                plan, task=(plan.task + "\n\n" if plan.task else "") + note
            )

    # TEMPORARY (claunch-z7b0n), during the v1 -> v2 migration: the run's
    # inputs.issue is the issue the board settled -- minted, taken or joined
    # -- and empty when there is none. The input stays declared so the
    # workflows keep their text, but the daemon's answer is the only one a
    # run starts with. Remove this, and beads.adopt_input_issue, once no v2
    # workflow declares an 'issue' input.
    if (plan.workflow_version == 2 and plan.cflow_inputs is not None
            and "issue" in plan.cflow_inputs):
        plan = replace(plan, cflow_inputs={
            **plan.cflow_inputs, "issue": linked["issue"] if linked else ""})

    opening = ""
    if plan.wanted:
        try:
            arranged, opening = await onboard.arrange(
                plan, name=name, cwd=cwd, mesh_mgr=_mesh_mgr(request)
            )
        except Exception:
            manager.discard(name)
            raise
        report.update(arranged)
    if session.sdef.prompt_packs:
        # Ahead of the arranged part, so the opening still ends with the task:
        # the packs are standing rules for the whole session, and the task is
        # what it acts on first.
        rules = prompt_packs.opening(session.sdef.prompt_packs)
        # A child's parent block still leads (onboard.arrange: the frame the
        # rest is read in); the packs follow it. Every spawned member with a
        # role now carries its role pack, so this is the common case.
        head = (onboard._parent_block(plan, channel=report.get("parent_channel"))
                if plan.parent and plan.wanted else "")
        if head and opening.startswith(head):
            rest = opening[len(head):].lstrip("\n")
            opening = head + "\n\n" + rules + ("\n\n" + rest if rest else "")
        else:
            opening = rules + ("\n\n" + opening if opening else "")
        report["prompt_packs"] = [p["id"] for p in session.sdef.prompt_packs]
        if packs_required:
            report["prompt_packs_required"] = packs_required
        shipped_copies = [
            p["id"] for p in session.sdef.prompt_packs
            if p.get("source") == prompt_packs.PACKAGE
        ]
        if shipped_copies:
            report["prompt_packs_shipped"] = shipped_copies
    if selected:
        # The goal is the head of the opening itself — one block either way
        # it travels (argv or typed in), never a slash command.
        opening = score_goal.prompt(session.sdef) + (
            "\n\n" + opening if opening else ""
        )
    images = opening_images.claim(image_ids, name)
    try:
        # An opening with images is typed in, never put on the command line:
        # the images go into the composer first and the text is submitted
        # with them (_opening_image_step), and a command-line prompt would be
        # submitted on its own before any image could join it.
        await manager.launch_async(session, opening="" if images else opening)
    except Exception:
        manager.discard(name)
        await onboard.unwind(report, name=name, cwd=cwd, mesh_mgr=_mesh_mgr(request))
        raise
    if images:
        report["opening_images"] = [str(img.path) for img in images]
        step, text = _opening_image_step(session, images, opening)
        onboard.open_with(session, text, before=step)
    else:
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
            # No run because the child's user said no (claunch-val3l.2).
            if sess.sdef.cflow_declined:
                entry["cflow_declined"] = dict(sess.sdef.cflow_declined)
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
        session = manager.stage_child(parent, body, warnings=warnings,
                                      created_by=actors.from_request(request))
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
        return json_response(onboard.error_body(exc), status=400)
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


async def h_session_detach(request: web.Request) -> web.Response:
    """End a session's parent/child relation: it becomes a root
    (claunch-mz09h).

    :meth:`SessionManager.detach` holds the rules (an ``actor`` must command
    the child; nobody detaches itself). Once the pair is no longer kin the
    mesh stops letting them past a cut edge, and the private channel the
    daemon opened for them is cut here; a team mesh both share keeps its own
    wiring. There is no re-attach beyond the operator's reparent.
    """
    manager: SessionManager = request.app["manager"]
    child = request.match_info["name"]
    body = await _json_body(request)
    try:
        result = manager.detach(child, actor=str(body.get("actor") or ""))
    except ManagerError as exc:
        msg = str(exc)
        if "no session named" in msg:
            return json_error(404, msg)
        if "does not command" in msg or "cannot detach itself" in msg:
            return json_error(403, msg)
        return json_error(400, msg)
    mm = request.app.get("mesh")
    result["cut"] = await mm.unlink_lineage(child, result["previous"]) if mm else []
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
            by = actors.with_reason(actors.from_request(request), "clear")
            for s in live:
                actors.note(s, by)
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
    by = actors.from_request(request)
    for session in list(manager.list()):
        if session.exited:
            continue
        name = session.sdef.name
        try:
            if await _winding_down(request, session, force=force, by=by):
                winding.append(name)
                continue
            manager.kill(name, force=force, by=by)
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
    by = actors.from_request(request)
    for session in list(manager.list()):
        if session.exited:
            continue
        name = session.sdef.name
        try:
            # A wind-down already in flight for this session is overtaken:
            # the pause is the second, immediate stop that the kill button
            # turns into while one runs.
            board.winddowns.pop(name, None)
            manager.pause(name, force=force, by=by)
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
            await manager.respawn_async(name)
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
        # A held record is a running session asleep (claunch-5l35i): not
        # done, so not archived in bulk; and it wakes on use, so a bulk
        # relaunch would only undo the hold.
        if getattr(session, "held_at", None):
            continue
        name = session.sdef.name
        try:
            await manager.respawn_async(name)
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
        # A held record is a running session asleep (claunch-5l35i): not
        # done, so not archived in bulk; and it wakes on use, so a bulk
        # relaunch would only undo the hold.
        if getattr(session, "held_at", None):
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


async def h_score_goal_defaults(request: web.Request) -> web.Response:
    return json_response({"enabled": bool(store.daemon_config().get("score_goal_default", False))})


async def h_score_goal_defaults_set(request: web.Request) -> web.Response:
    body = await _json_body(request)
    try:
        value = score_goal.enabled(body.get("enabled"))
    except ValueError as exc:
        return json_error(400, str(exc))
    store.set_daemon_field("score_goal_default", value)
    return json_response({"enabled": value})


async def h_role_workflows(request: web.Request) -> web.Response:
    """Per-role overrides of the workflow the session pickers auto-select
    (``daemon.role_workflows``), read live from the config file."""
    try:
        return json_response({"role_workflows": store.role_workflows()})
    except store.StoreError as exc:
        return json_error(500, str(exc))


async def h_role_workflows_set(request: web.Request) -> web.Response:
    """Set one role's auto-selected workflow, from the Settings page.

    Body ``{"role": <role>, "workflow": <value>}``: a workflow name,
    ``v2:<name>``, ``-`` for no auto-pick, or ``""``/null to drop the
    override and let the workflows' own ``default_role`` decide again. The
    workflow is not checked against a directory's list: which workflows
    exist depends on where a session stands, so a picker applies an
    override only where it is declared and falls back elsewhere.
    """
    body = await _json_body(request)
    try:
        role = mesh_roles._check_name(body.get("role"), "role")
    except mesh_roles.RoleError as exc:
        return json_error(400, str(exc))
    workflow = body.get("workflow")
    if workflow is not None and not isinstance(workflow, str):
        return json_error(400, "'workflow' must be a string or null")
    workflow = (workflow or "").strip()
    if workflow and (any(c.isspace() for c in workflow) or len(workflow) > 200):
        return json_error(400, f"invalid workflow value {workflow!r}")
    try:
        store.set_role_workflow(role, workflow or None)
        return json_response({"role_workflows": store.role_workflows()})
    except store.StoreError as exc:
        return json_error(500, str(exc))


async def h_session_score_goal(request: web.Request) -> web.Response:
    return json_response(score_goal.view(_session(request).sdef))


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
    """Pause or resume every repeating session reminder source for one session."""
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


async def h_session_reminder_note(request: web.Request) -> web.Response:
    """Set (or, with an empty value, clear) a session's temporary
    instructions (claunch-ozyje, :attr:`SessionDef.reminder_note`).

    Every session reminder delivered into the session carries the text as
    its last section until it is cleared. Refused on an exited session, like
    the other reminder levers: nothing would be delivered there.
    """
    manager: SessionManager = request.app["manager"]
    body = await _json_body(request)
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        return json_error(400, "'note' must be text, or null to clear it")
    session = _session(request)
    if session.exited:
        return json_error(409, f"session {session.sdef.name!r} has exited")
    try:
        session = manager.set_reminder_note(session.sdef.name, note or "")
    except ValueError as exc:
        return json_error(400, str(exc))
    return json_response(session.info())


async def _stats_call(app: web.Application, request: dict):
    """``(result, reader)`` for one stats request: answered by the stats
    worker process, or -- when it cannot answer -- read in a thread here.
    ``reader`` says which, so the page and a probe can tell."""
    worker = app.get("stats_worker")
    request = {**request, "offset": statsworker.zone_offset()}
    if worker is not None:
        try:
            return await worker.call(request), "worker"
        except statsworker.WorkerUnavailable as exc:
            log.warning("stats: %s; reading in the daemon", exc)
    return await asyncio.to_thread(statsworker.handle, request), "thread"


async def h_session_stats(request: web.Request) -> web.Response:
    """One session's usage over time and the origin of its input, for the
    dashboard's Stats page (``?unit=hour|day|week``, default day). See
    :mod:`sessionstats` for what each figure means and how it is estimated;
    the reading runs in the stats worker process (:mod:`statsworker`)."""
    manager: SessionManager = request.app["manager"]
    session = manager.get(request.match_info["name"])
    unit = request.query.get("unit", "day")
    if unit not in sessionstats.UNITS:
        return json_error(400, f"unit must be one of: {', '.join(sessionstats.UNITS)}")
    sdef = getattr(session, "sdef", None)
    head, path = await asyncio.to_thread(sessionstats.locate, sdef)
    try:
        reading, reader = await _stats_call(request.app, {
            "op": "read", "head": head,
            "path": str(path) if path is not None else None, "unit": unit})
    except statsworker.WorkerError as exc:
        return json_error(500, f"reading the transcript failed: {exc}")
    return json_response({**reading, "reader": reader})


async def h_stats_compare(request: web.Request) -> web.Response:
    """Several sessions side by side (``?sessions=a,b,c&unit=day``): each
    one's figures and their spread across the set -- mean, median, standard
    deviation, z-score and rank per session (:mod:`statscompare`)."""
    manager: SessionManager = request.app["manager"]
    unit = request.query.get("unit", "day")
    if unit not in sessionstats.UNITS:
        return json_error(400, f"unit must be one of: {', '.join(sessionstats.UNITS)}")
    names: list = []
    for name in request.query.get("sessions", "").split(","):
        name = name.strip()
        if name and name not in names:
            names.append(name)
    if not names:
        return json_error(400, "sessions: give at least one session name")
    if len(names) > statscompare.MAX_SESSIONS:
        return json_error(
            400, f"sessions: at most {statscompare.MAX_SESSIONS} at a time")
    sdefs = [getattr(manager.get(n), "sdef", None) for n in names]
    located = await asyncio.to_thread(lambda: [sessionstats.locate(d) for d in sdefs])
    items = [{"head": head, "path": str(path) if path is not None else None}
             for head, path in located]
    try:
        result, reader = await _stats_call(
            request.app, {"op": "compare", "items": items, "unit": unit})
    except statsworker.WorkerError as exc:
        return json_error(500, f"reading the transcripts failed: {exc}")
    return json_response({**result, "reader": reader})


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
        info["prs"] = session_prs.for_session(cwd, session.sdef.name)
        metering.attach(info, live=metering.live_index())
        tokenusage.attach(info, getattr(session, "sdef", None))
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
            "cflow2": None,
            "workflows": [],
        }
        if cwd:
            out["cflow"] = _clip_journal(_cflow_entry(manager, cwd, info["name"]))
            out["workflows"] = _startable_all(cwd)
            # The v2 run this session drives, if any: its own slot
            # (.cflow2/runs/<name>/), read beside the v1 one.
            out["cflow2"] = cflow2_web.run_summary(cwd, info["name"])
        return out

    async def commits():
        # In a thread: this is a git walk over every ref, and the detail
        # panel polls. A pathological repository must cost this response,
        # never the whole daemon's loop.
        if not cwd:
            return None
        return session_commits.summary(
            await asyncio.to_thread(session_commits.for_session, cwd, session.sdef.name)
        )

    # The three slow reads do not depend on each other, so they run side by
    # side: the response waits for the slowest of them, not their sum
    # (claunch-74t39). The board's listing may be one poll old -- the panel
    # polls past its cache, and waiting on a ``br`` fork of the whole board
    # was most of a poll's time.
    body, beads_view, commit_summary = await asyncio.gather(
        asyncio.to_thread(describe),
        request.app["beads"].session_view(session, stale_ok=True),
        commits(),
    )
    info = body["session"]
    prog = manager.progress.get(info.get("name") or "")
    if prog:
        info["progress"] = prog
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
        "beads": beads_view,
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
        "commits": commit_summary,
        # The pending merge/handoff on this session, if any — the detail
        # panel's Hand off box reads it to show the request and its cancel.
        "handoff": request.app["handoff"].state(name),
    })
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


async def h_session_window(request: web.Request) -> web.Response:
    """The measurement window as it concerns this session (claunch-bwzkt):
    the grant it holds (elapsed, and the remaining time the kept history
    suggests), the place it waits in the queue and the rule that keeps it
    there, and the machine's load in a few numbers. Polled by the terminal
    header's window strip; a read, like ``GET /api/window``."""
    session = _session(request)
    return json_response(request.app["window"].session_view(session.sdef.name))


async def h_session_timers(request: web.Request) -> web.Response:
    """Every countdown this session waits on, soonest first (claunch-7dzae):
    a cflow v2 choice held until its interval window opens, a v2 wait, a
    loop pause, an ask deadline -- whatever the providers registered in
    ``session_timers`` report. Polled by the terminal's timer line
    (#term-timers); a read."""
    session = _session(request)
    data = await asyncio.to_thread(
        session_timers.collect, session.sdef.name, _session_cwd(session), app=request.app)
    return json_response(data)


async def h_session_procs(request: web.Request) -> web.Response:
    """The processes the daemon started for this session (a restart
    command) that are still running, with what they started -- the
    terminal's process line (#term-procs) polls it (claunch-ihwel)."""
    session = _session(request)
    found = session_procs.tracker()
    await asyncio.to_thread(found.refresh)
    return json_response({"session": session.sdef.name,
                          "procs": found.listing(session.sdef.name)})


async def h_session_proc_kill(request: web.Request) -> web.Response:
    """End one of this session's tracked processes and everything under it.
    Only a pid the tracker holds for this session is signalled: any other
    is 404, so this route cannot be used to stop an arbitrary process."""
    session = _session(request)
    try:
        pid = int(request.match_info["pid"])
    except ValueError:
        return json_error(400, "pid must be an integer")
    found = session_procs.tracker()
    try:
        killed = await asyncio.to_thread(found.kill, session.sdef.name, pid)
    except session_procs.NotAMember as exc:
        return json_error(404, str(exc))
    return json_response({"session": session.sdef.name, "killed": killed,
                          "procs": found.listing(session.sdef.name)})


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


async def h_session_mcp_ready(request: web.Request) -> web.Response:
    """``claunch mcp`` of this session finished its handshake (claunch-xixag).

    The opening of a session with MCP servers waits for it
    (:mod:`mcp_ready`); recording it twice is harmless.
    """
    session = _session(request)
    session.note_mcp_ready()
    return json_response({"ready": True})


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

    A *paused* record is the one exited state a kill still changes: the
    process is already gone, but the record is filed as a temporary stop.
    Killing it clears the marker (:meth:`SessionManager.unpause`) so it reads
    as killed and leaves the bulk resume's set; the reply carries
    ``unpaused: true`` beside ``already_exited``.

    ``?force=1`` is SIGKILL, as it always was, and means nothing on an exited
    session — there is nothing left for it to carry. The mesh is untouched
    either way: a member row is *meant* to outlive the terminal, reading
    ``exited``.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    force = request.query.get("force") in ("1", "true")
    session = manager.get(name)  # ManagerError -> 400, as it always did
    by = actors.from_request(request)
    if session.exited:
        unpaused = manager.unpause(name, by=by)
        # A held record is filed as killed the same way, and the exit hooks
        # the hold put off run now (SessionManager.unhold).
        unheld = manager.unhold(name, by=by)
        body = {**session.info(), "already_exited": True}
        if unpaused:
            body["unpaused"] = True
        if unheld:
            body["unheld"] = True
        return json_response(body)
    if await _winding_down(request, session, force=force, by=by):
        return json_response({**session.info(), "winding_down": True})
    session = manager.kill(name, force=force, by=by)
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
    session = manager.pause(name, force=force, by=actors.from_request(request))
    return json_response(session.info())


async def h_session_idle_hold(request: web.Request) -> web.Response:
    """Hold a running session now (claunch-5l35i): end its program, keep the
    session. What the idle clock (``daemon/hold.py``) does on its own after
    ``hold_idle_minutes``, asked for by hand. No wind-down: nothing is ending
    for the board. 400 for a session a respawn would bring back blank.

    On an exited session the reply says ``already_exited``, as pause does.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    force = request.query.get("force") in ("1", "true")
    session = manager.get(name)
    if session.exited:
        return json_response({**session.info(), "already_exited": True})
    session = manager.hold(name, force=force, reason="manual",
                           by=actors.from_request(request))
    return json_response(session.info())


async def h_session_wake(request: web.Request) -> web.Response:
    """Bring a held session back now. The manual twin of the wake an attach,
    a mesh message or a cflow notice does; anything not held is returned as
    it is (``woken: false``). A killed or paused record is the respawn
    route's, not this one's."""
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    before = manager.get(name)
    held = bool(before.exited and getattr(before, "held_at", None))
    session = await manager.wake_async(name, by="api")
    return json_response({**session.info(), "woken": held})


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
    """File a session in the archive, ending it first if it is still running.

    Archive is available at every moment of a session's life, not only after
    it has ended. On a live session this route IS the ending: the program is
    terminated and the record is filed in the one call, so an operator who
    has decided a session is finished presses once instead of pressing kill,
    watching for the exit to land, and pressing archive.

    Nothing waits in between. The board wind-down that a first kill turns
    into is dropped rather than run (:func:`_winding_down` is not consulted
    here), because it asks the session for one more turn and a session being
    archived has none left to give; a pending merge or handoff is forgotten
    for the same reason the kill route forgets it — the operator pressed past
    it. The issues the session held are released by the daemon's own exit
    sweep, exactly as they are for a kill.

    ``?force=1`` is SIGKILL rather than a graceful stop. The reply carries
    ``stopped: true`` when this call was the one that ended the session, so a
    caller can tell "archived a record that was already dead" from "ended a
    running session and archived it" — the second is the destructive one, and
    a UI that cannot tell them apart cannot warn about it.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    force = request.query.get("force") in ("1", "true")
    session = manager.get(name)  # ManagerError -> 400, as it always did
    if session.exited:
        return json_response(manager.archive(name).info())
    request.app["handoff"].forget(name)
    request.app["beads"].winddowns.pop(name, None)
    session = await manager.stop_and_archive(
        name, force=force, by=actors.from_request(request))
    return json_response({**session.info(), "stopped": True})


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


async def h_session_note(request: web.Request) -> web.Response:
    """Set (or, with an empty value, clear) a session's user note.

    The note is the *operator's* annotation on a terminal — why this one is
    being kept around — so it lives on the session record and is read back by
    the rail row, the session header and the detail panel (and by search).
    Nothing sends it into the session: it is the person's, not the agent's,
    which is what keeps it usable as a reminder to self rather than an
    instruction. Unlike the reminder and score levers this one is deliberately
    settable on a session that has already exited, since "why am I keeping
    this record" is asked most often about one that is gone.
    """
    manager: SessionManager = request.app["manager"]
    body = await _json_body(request)
    note = body.get("note")
    if note is not None and not isinstance(note, str):
        return json_error(400, "'note' must be text, or null to clear it")
    try:
        session = manager.set_note(request.match_info["name"], note or "")
    except ValueError as exc:
        return json_error(400, str(exc))
    return json_response(session.info())


async def h_session_cflow_declined(request: web.Request) -> web.Response:
    """Record (POST) or withdraw (DELETE) a user's "no" to a cflow run for
    this session (claunch-val3l.2, :attr:`SessionDef.cflow_declined`).

    POST takes ``{"workflow", "reason", "by"}``, all optional text: the
    workflow that was offered, the user's answer in their words, and who
    recorded it (the CLI sends the calling session). The record is what a mesh
    leader reads -- on the roster and on ``children`` -- to tell a worker that
    runs no cflow by its user's decision from one whose run is missing.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    if request.method == "DELETE":
        return json_response(manager.clear_cflow_declined(name).info())
    body = await _json_body(request)
    fields = {}
    for key in ("workflow", "reason", "by"):
        value = body.get(key)
        if value is not None and not isinstance(value, str):
            return json_error(400, f"'{key}' must be text")
        fields[key] = value or ""
    return json_response(manager.set_cflow_declined(name, **fields).info())


async def h_session_progress(request: web.Request) -> web.Response:
    """What one session last published about its progress (daemon/progress.py)."""
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    try:
        manager.get(name)
    except ManagerError:
        return json_error(404, f"no session named {name!r}")
    return json_response({"session": name, "progress": manager.progress.get(name)})


async def h_session_progress_set(request: web.Request) -> web.Response:
    """Publish a session's progress from outside its terminal.

    The body is the record itself (``stage``, ``done``, ``total``,
    ``active``, ``text``, ``file``, ``url``) and replaces the previous one;
    ``{}`` or ``null`` clears it. The same store the terminal's
    ``777;claunch;progress;`` sequence writes, so a run may use either. An
    exited session takes one too: a supervisor reports how its run ended
    after the terminal is gone. Not parsed with :func:`_json_body`, which
    reads a malformed body as ``{}`` -- here that would clear.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    try:
        manager.get(name)
    except ManagerError:
        return json_error(404, f"no session named {name!r}")
    try:
        raw = await request.json()
    except Exception:
        return json_error(400, "progress must be a JSON object (or null to clear)")
    try:
        stored = manager.progress.set(name, raw or None)
    except ValueError as exc:
        return json_error(400, str(exc))
    return json_response({"session": name, "progress": stored})


async def h_session_progress_clear(request: web.Request) -> web.Response:
    """Clear a session's published progress."""
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    try:
        manager.get(name)
    except ManagerError:
        return json_error(404, f"no session named {name!r}")
    manager.progress.set(name, None)
    return json_response({"session": name, "progress": None})


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


async def _winding_down(request: web.Request, session, *, force: bool,
                        by: Optional[dict] = None) -> bool:
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
    return await request.app["beads"].begin_winddown(
        session, request.app["manager"], by=by)


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
    is staged with ``fork: true`` (Claude's ``--resume --fork-session`` or
    Codex's ``fork`` of the parent's pinned conversation), so it is a child of this session and
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
    try:
        out = await stage_quick_fork(
            request, request.match_info["name"], await _json_body(request)
        )
    except QuickForkError as exc:
        return json_error(exc.status, str(exc))
    return json_response(out, status=201)


def _quick_fork_run(workflow: str, version: int, *, parent: str, cwd: str) -> Tuple[str, int]:
    """``(name, version)`` of the run a quick-fork is started on, for its
    marker: ``("", 0)`` for none, version 0 when it cannot be told.

    A "." is resolved the way the child's inheritance will resolve it -- the
    origin is the copy's parent there -- so the marker can name the run
    instead of leaving it out. Nothing here changes what is started; a
    reading that fails only costs the marker its tool names.
    """
    if workflow == onboard.NO_WORKFLOW:
        return "", 0
    if workflow == ".":
        try:
            return onboard._paired(parent=parent, parent_cwd=cwd, cwd=cwd)
        except Exception as exc:  # noqa: BLE001 - the marker only
            log.debug("could not read %r's paired workflow: %s", parent, exc)
            return "", 0
    if version in (1, 2):
        return workflow, version
    try:
        chosen = cflow2_web.choose(workflow, None, cwd=cwd, roots=(cwd,))
    except Exception as exc:  # noqa: BLE001 - onboarding reports the real error
        log.debug("could not tell %r's cflow version: %s", workflow, exc)
        return workflow, 0
    return workflow, 2 if chosen is not None else 1


def _origin_run_name(origin: str, cwd: str) -> str:
    """The workflow of the run ``origin`` drives in ``cwd`` (v2 first, then
    v1), or "" -- named in the copy's marker so the copy can tell that run's
    steps in the conversation it inherited from its own run's."""
    if not cwd:
        return ""
    state = cflow2_web.live_run(cwd, origin)
    if state:
        return str(state.get("workflow") or "")
    try:
        status = cflow_engine.status(cwd, scope=origin)
    except Exception as exc:  # noqa: BLE001 - the marker only
        log.debug("no v1 run to read for %r: %s", origin, exc)
        return ""
    if not status or status.get("status") in ("idle", "done", "aborted"):
        return ""
    return str(status.get("workflow") or "")


class QuickForkError(Exception):
    """A quick-fork that cannot be made, with the HTTP status it answers."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status


async def stage_quick_fork(
    request: web.Request, parent: str, body: dict, *, tag: str = "qf",
    spawn_extra: Optional[dict] = None, before_launch=None,
    self_merge_allowed: Optional[bool] = None,
    created_by: Optional[dict] = None,
) -> dict:
    """The quick-fork itself, shared by the operator's press (above) and the
    ``deep_think`` tool (daemon/deep_think.py), which forks the asking
    session the same way and adds its own fields: ``tag`` names the copy
    (``<parent>-<tag><n>``), ``spawn_extra`` rides the spawn body (``model``
    and ``effort``, under the spawn policy like any child's), and
    ``before_launch(session)`` writes onto the staged copy before it starts.
    ``self_merge_allowed`` (the body's field when not given) lets the copy
    merge without the operator's press; it is written onto the staged record
    here rather than carried by the spawn body, so a plain spawn has no way
    to set it.
    Raises :class:`QuickForkError` with the status the route answers.
    """
    manager: SessionManager = request.app["manager"]
    try:
        origin = manager.get(parent)
    except ManagerError as exc:
        raise QuickForkError(404, str(exc)) from None
    if origin.exited:
        raise QuickForkError(400, f"session {parent!r} has exited — an exited session cannot be quick-forked")
    if not spawn_mod.can_fork(origin.sdef.to_dict()):
        raise QuickForkError(
            400,
            f"session {parent!r} has no conversation to copy: quick-fork needs "
            "a Claude or Codex session whose conversation is on disk (its first turn "
            "has landed)",
        )
    name = str(body.get("name") or "").strip() or handoff_mod.fork_name(
        parent, {s.sdef.name for s in manager.list()}, tag=tag,
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
    role = str(body.get("role") or "").strip()
    # Sibling: the copy answers to the origin's parent (a root when the origin
    # is one) instead of the origin. It is still BUILT from the origin -- its
    # conversation, its directory -- which is what stage_child's place_under
    # keeps apart.
    sibling = body.get("sibling") in (True, "true", "1", 1)
    if self_merge_allowed is None:
        self_merge_allowed = body.get("self_merge_allowed") in (True, "true", "1", 1)
    placed = (origin.sdef.parent or "") if sibling else parent
    mesh_mgr = _mesh_mgr(request)
    origin_meshes = [
        row for row in mesh_mgr.meshes_for_session(parent)
        if not row.get("parent_channel")
    ]
    if mesh == ".":
        # The origin's mesh, read here rather than by inherit_mesh downstream
        # when it can be named outright: inherit_mesh reads the PARENT's, and
        # a sibling's parent is not the origin. With none or several the child
        # keeps the downstream reading (it opens one, or refuses naming them);
        # a sibling has no such parent to fall back on.
        if len(origin_meshes) == 1:
            mesh = origin_meshes[0]["mesh"]
        elif sibling:
            if origin_meshes:
                raise QuickForkError(400, (
                    f"session {parent!r} is in {len(origin_meshes)} meshes "
                    f"({', '.join(sorted(m['mesh'] for m in origin_meshes))}) "
                    "— name the one the copy joins"
                ))
            mesh = onboard.NO_MESH
    if workflow == "." and sibling:
        # The same pair a child would get (inherit_workflow), read from the
        # origin's run, because the sibling's parent is not the origin.
        paired, version = onboard._paired(
            parent=parent, parent_cwd=origin.sdef.cwd or "",
            cwd=origin.sdef.cwd or "",
        )
        workflow = paired or onboard.NO_WORKFLOW
        workflow_version = version if paired else 0
    else:
        workflow_version = 0
    inherit_mesh = mesh == "."
    inherit_workflow = workflow == "."
    # What the marker says about the run, which the spawn body does not
    # settle: the name an inherited one resolves to (the same pair the
    # child's inherit_workflow reads, the origin being its parent), the
    # version whose tools drive it, and the origin's own run, which the copy
    # must not mistake for its own (claunch-ychal).
    marker_workflow, marker_version = _quick_fork_run(
        workflow, workflow_version, parent=parent, cwd=str(origin.sdef.cwd or ""),
    )
    origin_workflow = _origin_run_name(parent, str(origin.sdef.cwd or ""))
    named_mesh = "" if mesh in (onboard.NO_MESH, ".") else mesh
    # The origin's role where the copy lands (or, with no mesh, its only one),
    # and the copy's own: asked for, or what the join will resolve its name to.
    origin_role = next(
        (m["role"] for m in origin_meshes if m["mesh"] == named_mesh), ""
    ) if named_mesh else (
        origin_meshes[0]["role"] if len(origin_meshes) == 1 and mesh == onboard.NO_MESH else ""
    )
    fork_role = role
    if named_mesh and not fork_role:
        try:
            fork_role = mesh_mgr.get(named_mesh).roleset.resolve(name, "")
        except Exception:
            fork_role = ""
    opening = handoff_mod.compose_marker(
        origin=parent, fork=name, marker=marker, forked_at=forked_at,
        # The origin's own directory, because a fork cannot be moved out of
        # it. Named in the block so the copy can see what it shares.
        cwd=str(origin.sdef.cwd or ""),
        # An inherited one the origin's roster could not name is not named
        # here: it is settled downstream, and the block would be guessing.
        mesh=named_mesh,
        workflow=marker_workflow,
        role=fork_role, origin_role=origin_role,
        sibling=sibling, placed_under=placed,
        workflow_version=marker_version, origin_workflow=origin_workflow,
        self_merge_allowed=self_merge_allowed,
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
    # The "none" spellings are read by the inheritance step, which only runs
    # for a session with a parent; a root says none by leaving the field out.
    if not inherit_mesh and (placed or mesh != onboard.NO_MESH):
        spawn_body["mesh"] = mesh
    if not inherit_workflow and (placed or workflow != onboard.NO_WORKFLOW):
        spawn_body["workflow"] = workflow
    if workflow_version == 2:
        spawn_body["workflow_version"] = 2
    if role:
        # Without a mesh preflight refuses it ("only mean something with a
        # mesh"); with one, it refuses an exclusive role (leader) a live
        # member already holds -- the authority behind the form's check.
        spawn_body["role"] = role
    for key, value in (spawn_extra or {}).items():
        spawn_body.setdefault(key, value)
    warnings: list = []
    try:
        session = manager.stage_child(
            parent, spawn_body, warnings=warnings,
            place_under=None if not sibling else placed,
            created_by=created_by or actors.from_request(request),
        )
    except spawn_mod.SpawnDenied as exc:
        raise QuickForkError(403, str(exc)) from None
    except (HarnessError, ValueError, TypeError) as exc:
        raise QuickForkError(400, f"bad quick-fork request: {exc}") from None
    except ManagerError as exc:
        raise QuickForkError(409 if "already exists" in str(exc) else 400, str(exc)) from None
    if self_merge_allowed:
        session.sdef = replace(session.sdef, self_merge_allowed=True)
        manager.persist()
    if before_launch is not None:
        before_launch(session)
    try:
        result = await _onboard_and_launch(
            request, session, spawn_body, parent=(placed or None),
        )
    except onboard.OnboardError as exc:
        raise QuickForkError(400, str(exc)) from None
    except (HarnessError, ValueError, TypeError) as exc:
        raise QuickForkError(400, f"bad quick-fork request: {exc}") from None
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
        # Where the copy hangs: under the origin, or beside it ("" = a root).
        "sibling": sibling,
        "parent": placed,
        "role": fork_role if named_mesh else "",
    }
    if warnings:
        out["warnings"] = warnings
    return out


async def h_session_handoff(request: web.Request) -> web.Response:
    """Request a merge/handoff from this session, or complete one.

    With ``text`` in the body this is the completion — the agent handing in
    its wrap-up (the MCP ``handoff`` tool and the two CLI verbs post here
    from inside the session): the report is delivered to the target and then
    the session is ended, in that order. Without ``text`` it is the
    operator's request: the instruction block is typed into the session and
    the request is recorded as pending (the row's ``handoff`` field).

    ``to`` names the target for a handoff; a quick-fork's merge needs none
    (it goes back to the origin, or to the nearest live session above it
    when the origin has ended) and refuses any other. ``kind`` may force
    ``handoff`` on a quick-fork that wants to report elsewhere. ``actor`` is
    the session filing a request (the CLI sends ``$CLAUNCH_SESSION``); a
    quick-fork's request is the operator's, so one with an actor is refused
    unless the copy may merge on its own (see ``Handoffs.request``).
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    body = await _json_body(request)
    to = str(body.get("to") or "").strip()
    kind = str(body.get("kind") or "").strip()
    text = body.get("text")
    actor = str(body.get("actor") or "").strip()
    handoffs: handoff_mod.Handoffs = request.app["handoff"]
    try:
        if text is not None and str(text).strip():
            result = await handoffs.complete(manager, name, text=str(text), to=to, kind=kind)
            # A merge with no live session above the copy is answered, not
            # refused: the wrap-up was saved and the copy kept (``note`` says
            # where), and ``completed`` is false because nothing landed.
            return json_response({"completed": not result.get("undelivered"), **result})
        result = await handoffs.request(manager, name, to=to, kind=kind, actor=actor)
        return json_response({"requested": True, "source": name, **result})
    except handoff_mod.HandoffError as exc:
        msg = str(exc)
        return json_error(404 if "no session named" in msg else 400, msg)


async def h_session_self_merge(request: web.Request) -> web.Response:
    """Set (or, with ``?off=1``, clear) whether a quick-fork may merge on its
    own (:attr:`SessionDef.self_merge_allowed`).

    The operator's lever, like the merge press it stands in for: a body
    ``actor`` naming the fork itself is refused, since a copy that could
    grant itself the flag would need no press at all.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    body = await _json_body(request)
    on = request.query.get("off") not in ("1", "true")
    try:
        session = manager.set_self_merge_allowed(
            name, on, actor=str(body.get("actor") or "").strip(),
        )
    except ManagerError as exc:
        msg = str(exc)
        return json_error(404 if "no session" in msg.lower() else 400, msg)
    return json_response({**session.info(), "self_merge_allowed": bool(on)})


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
    # The parent named in the path, unless the request says the parent's
    # run did it (the cflow2 orphan reap sends ``cflow:<parent>``).
    by = actors.parse_header(request.headers.get(actors.HEADER)) or actors.make(
        actors.AGENT, parent)
    if await _winding_down(request, target, force=force, by=by):
        return json_response({**target.info(), "winding_down": True})
    session = manager.kill(child, force=force, by=by)
    return json_response(session.info())


async def h_session_respawn(request: web.Request) -> web.Response:
    manager: SessionManager = request.app["manager"]
    session = await manager.respawn_async(request.match_info["name"])
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
            # Unnamed, it is named after the session moving into it -- not
            # after the pane in this daemon's environment (claunch-2yiij.3).
            wt = worktree_mod.create(
                root, str(wt_name).strip() or worktree_mod.default_name(token=name)
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
    ``{"null_token": true}`` for none at all (``--null``). These are one
    choice — picking any clears the others, so a borrow set on a ``--null``
    session turns the token back on. Claude sessions may also pass
    ``disable_artifact_tool: true|false`` to disable or restore the Artifact
    tool in the same restart. When auth fields are omitted, their current
    choice is retained, allowing the tool setting to be changed by itself.
    The restart is
    :meth:`SessionManager.reborrow` — stop, relaunch under the definition
    with the auth swapped. The directory does not move, so the conversation
    stays filed where it always was and there is nothing to carry.

    Operator-only, like migrate and respawn: no agent-facing route reaches
    this. An agent's auth is its spawner's arrangement, changed by the human
    or at spawn time, never by the agent mid-run.
    """
    manager: SessionManager = request.app["manager"]
    body = await _json_body(request)
    disable_artifact_tool = body.get("disable_artifact_tool")
    if "disable_artifact_tool" in body and not isinstance(
        disable_artifact_tool, bool
    ):
        return json_error(400, "'disable_artifact_tool' must be a boolean")
    if (
        "borrow" not in body and "null_token" not in body
        and "disable_artifact_tool" not in body
    ):
        return json_error(
            400,
            "pass 'borrow', 'null_token', or 'disable_artifact_tool'",
        )
    borrow = body.get("borrow")
    if borrow is not None and not isinstance(borrow, str):
        return json_error(400, "'borrow' must be a profile name or null")
    null_token = body.get("null_token", False)
    if not isinstance(null_token, bool):
        return json_error(400, "'null_token' must be a boolean")
    current = manager.get(request.match_info["name"])
    has_auth_choice = "borrow" in body or "null_token" in body
    if not has_auth_choice:
        borrow = current.sdef.borrow
        null_token = current.sdef.null_token
    session = await manager.reborrow(
        request.match_info["name"], borrow, null_token=null_token,
        disable_artifact_tool=disable_artifact_tool,
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


async def h_session_runtime_mode(request: web.Request) -> web.Response:
    """Restart a session with its YOLO and Sandbox switches rewritten.

    Body: ``{"yolo": true|false, "sandbox": true|false}`` — both, always: the
    detail panel turns one and sends the other as it reads now, so a
    request names the whole mode and never leans on what the daemon would
    infer. The change is :meth:`SessionManager.runtime_mode`.

    Operator-only, like skip-permissions.
    """
    manager: SessionManager = request.app["manager"]
    body = await _json_body(request)
    yolo, sandbox = body.get("yolo"), body.get("sandbox")
    if not isinstance(yolo, bool) or not isinstance(sandbox, bool):
        return json_error(
            400, "pass both 'yolo' and 'sandbox' as true or false"
        )
    session = await manager.runtime_mode(
        request.match_info["name"], yolo, sandbox
    )
    return json_response(session.info())


async def h_session_auto_compact(request: web.Request) -> web.Response:
    """Restart a claude session with auto-compact on or off, and its
    threshold pinned or returned to the profile's.

    Body: ``{"enabled": true|false, "window": <tokens>|null}``. ``window``
    omitted or null drops the session's own pin. The change is
    :meth:`SessionManager.auto_compact` — an edit of the definition's env
    and a relaunch, so the conversation, directory and auth are untouched.
    Operator-only, like skip-permissions.
    """
    manager: SessionManager = request.app["manager"]
    body = await _json_body(request)
    try:
        enabled, window = auto_compact_mod.parse(body)
    except ValueError as exc:
        return json_error(400, str(exc))
    session = await manager.auto_compact(
        request.match_info["name"], enabled, window
    )
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
    """Store one pasted image for a session and hand it to the harness.

    The web session line cannot hand a harness an *attachment*: the program in
    the PTY reads bytes, and an image is not bytes it can read as a prompt.
    This route stores the file and uses the harness's declared image
    transport: a path paste into its terminal, or the host clipboard and
    an image paste keystroke (see :func:`_deliver_pasted_image`).

    The path used to go into the browser's text composer. That only worked for a
    browser on the daemon's own machine: a person connecting from another PC
    was handed a path to a file that does not exist on their side, and nothing
    in the round trip told them so.

    The file is kept either way. It is how the clipboard gets the bytes, and
    it is the only record of what was handed over.

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
    delivered, keys, reason = await _deliver_pasted_image(session, target, kind)
    return json_response({
        "ok": True,
        "path": str(target),
        "bytes": total,
        "delivered": delivered,
        "keys": keys,
        "reason": reason,
    })


async def h_opening_image_upload(request: web.Request) -> web.Response:
    """Stage one image for the opening of a session not created yet.

    The create form's Opening task takes images (claunch-jmrmc). The create
    request is JSON, so each image is uploaded here first, as its own raw
    body with its media type in ``Content-Type`` -- the paste route's shape
    and size ceiling -- and the create request names the returned ids in
    ``opening_images``. Staging and claiming are
    :mod:`claude_launcher.daemon.opening_images`.
    """
    kind = (request.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    if kind not in opening_images.TYPES:
        return json_error(
            415,
            f"{kind or 'no Content-Type'} is not an image this accepts "
            f"({', '.join(sorted(opening_images.TYPES))})",
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
    image_id = await asyncio.to_thread(opening_images.stage, chunks, kind)
    return json_response({"id": image_id, "bytes": total, "type": kind}, status=201)


def _opening_image_ids(body: dict, harness: str) -> List[str]:
    """``body["opening_images"]`` checked, as staged ids.

    Refused for a harness that declares no image transport: the images
    could be stored but never handed over, and a session told about a picture
    it never received starts work on a wrong premise.
    """
    try:
        ids = opening_images.check_ids(body.get("opening_images"))
    except opening_images.OpeningImageError as exc:
        raise onboard.OnboardError(str(exc)) from None
    if ids:
        entry = harness_registry.registry().get(harness or "")
        if not (entry and entry.takes_images):
            raise onboard.OnboardError(
                f"harness {harness or 'unknown'!r} declares no image paste key "
                f"(image_paste_keys) or path transport, so it cannot be handed opening images"
            )
    return ids


def _opening_image_step(session, images, opening: str):
    """The pre-paste step of an opening that carries images, and its text.

    Each image goes into the composer the way the session line's paste does
    (:func:`_deliver_pasted_image`), in order; then the step waits out the
    settle window, so the harness has read the last image off the clipboard
    before the text is pasted behind it and submitted. Runs once: a delivery
    retried after the images went in does not paste them a second time.

    The text is a factory :meth:`Session.deliver` calls after the step. It
    names every image that could not be handed over, with the stored file's
    path, so the agent knows a picture it was meant to see is missing and
    where it is. An opening with no task text still says that images were
    attached: an empty text would cancel the delivery.
    """
    done = {"ran": False}
    failed: List[str] = []
    entry = harness_registry.registry().get(session.sdef.harness or "")
    marker = (
        re.compile(entry.image_paste_marker)
        if entry and entry.image_paste_marker else None
    )

    async def step() -> None:
        if done["ran"]:
            return
        done["ran"] = True
        for img in images:
            seen = _marker_count(session, marker) if marker else 0
            delivered, _keys, reason = await _deliver_pasted_image(
                session, img.path, img.media_type
            )
            if not delivered:
                log.warning(
                    "opening image %s for %r not handed over: %s",
                    img.path, session.sdef.name, reason,
                )
                failed.append(f"- {img.path} ({reason})")
            elif marker and not await _await_marker(session, marker, seen):
                log.warning(
                    "opening image %s for %r: no attachment placeholder "
                    "within %.0fs; pasting the text anyway",
                    img.path, session.sdef.name, IMAGE_ATTACH_TIMEOUT,
                )
        wait = _paste_settle["until"] - asyncio.get_running_loop().time()
        if wait > 0:
            await asyncio.sleep(wait)

    def text() -> str:
        parts = [opening] if opening else [
            f"{len(images)} image(s) attached to this opening message."
        ]
        if failed:
            parts.append(
                "These opening images could not be pasted here; the files "
                "are on this machine:\n" + "\n".join(failed)
            )
        return "\n\n".join(parts)

    return step, text


#: How long an opening waits for the harness to show that it took one image
#: (``image_paste_marker``) before it goes on without that evidence.
IMAGE_ATTACH_TIMEOUT = 10.0


def _marker_count(session, marker) -> int:
    return sum(len(marker.findall(line)) for line in session.capture())


async def _await_marker(session, marker, seen: int) -> bool:
    """Wait until the screen shows more ``marker`` matches than ``seen``.

    A pasted image is taken after the paste event: Claude Code reads the
    pasted path and only then draws ``[Image #N]``. Text pasted and submitted
    before that goes out as a message of its own, and the image is left in an
    empty composer (claunch-1lfrh). Counting, rather than looking for any
    match, keeps the second of two images from being satisfied by the first.
    """
    deadline = time.monotonic() + IMAGE_ATTACH_TIMEOUT
    while not session.exited:
        if _marker_count(session, marker) > seen:
            return True
        if time.monotonic() >= deadline:
            return False
        await asyncio.sleep(0.1)
    return False


#: Held across the clipboard write AND the keystroke that reads it back. The
#: clipboard is one per machine while sessions are many, so without this two
#: pastes racing each other would hand one session the other's image -- the
#: one failure of this route that produces a wrong result instead of an error.
_paste_image_lock = asyncio.Lock()

#: How long the clipboard is left alone after a paste key was sent, before
#: the next image may overwrite it. Sending the key is not the harness reading
#: the clipboard: Claude Code on Windows starts a PowerShell to do that, which
#: takes the better part of a second, so an image written right behind the key
#: can be the one it reads -- the second of two pictures pasted twice, the
#: first never. Several images in a row (the web file picker takes many at
#: once) are exactly that case. The wait is paid by the NEXT paste, and only
#: when it follows within this window; a single paste answers at once. It is
#: machine-wide on purpose, like the lock: a paste into another session
#: overwrites the same clipboard, so it waits too.
PASTE_SETTLE_SECONDS = 3.0

#: ``loop.time()`` before which the clipboard is still being read. Guarded by
#: ``_paste_image_lock``.
_paste_settle = {"until": 0.0}


async def _deliver_pasted_image(session, path, media_type: str):
    """Deliver a stored image using the harness's declared paste transport.

    Answers ``(delivered, keys, reason)``. A failure here is not an error
    response: the file is stored by the time this runs, so the useful answer
    is "stored, not delivered, because X" -- which the web session line shows
    where the person who pressed the key is looking.

    The path transport sends a path in one bracketed paste, which a
    supporting harness converts into an attachment without the clipboard.
    For the clipboard transport, the image keystroke is a property of the
    harness, so it comes from its declaration (``image_paste_keys``) rather
    than from a name check here. An undeclared harness gets nothing sent: a
    chord that means "paste an image" to one program means something else to
    another, and guessing would type that something else into a session.
    """
    entry = harness_registry.registry().get(session.sdef.harness or "")
    if not (entry and entry.takes_images):
        return False, [], (
            f"harness {session.sdef.harness or 'unknown'!r} has no image paste "
            f"key or path transport declared, so the image was stored but not handed over"
        )
    if entry and entry.image_paste_transport == "path":
        # A paste event lets the harness recognize the whole filename, even
        # with spaces. Never send Enter: attaching must preserve the draft.
        if not session.screen.bracketed_paste:
            return False, [], "the session is not ready for an image path paste"
        target = Path(path).resolve()
        pasted_path = (str(target) if entry.image_paste_path_format == "native"
                       else target.as_uri())
        try:
            await session.paste(pasted_path, enter=False)
        except Exception as exc:
            return False, [], f"the image path did not reach the session: {exc}"
        return True, [], ""
    keys = list(entry.image_paste_keys)
    loop = asyncio.get_running_loop()
    async with _paste_image_lock:
        wait = _paste_settle["until"] - loop.time()
        if wait > 0:
            await asyncio.sleep(wait)
        try:
            await clipboard.put_image(path, media_type)
        except clipboard.ClipboardError as exc:
            return False, [], str(exc)
        try:
            await session.send_keys(keys, force=True)
        except Exception as exc:  # the PTY is gone, or the session refused
            return False, keys, f"the keystroke did not reach the session: {exc}"
        _paste_settle["until"] = loop.time() + PASTE_SETTLE_SECONDS
    return True, keys, ""


def _keys_payload(session, nbytes: int, **extra) -> dict:
    out = {"ok": True, "bytes": nbytes}
    out.update(extra)
    if session.sdef.score_goal:
        out["score_goal"] = score_goal.view(session.sdef)
    return out


def _apply_score_feedback(request, session, kind: str) -> None:
    changes = score_goal.apply(session.sdef, kind)
    if changes:
        session.sdef = replace(session.sdef, **changes)
        request.app["manager"].persist()


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
    # One point of operator feedback may ride with an input send. It applies
    # only when that send actually lands — never on a duplicate replay and
    # never when nothing was delivered.
    try:
        feedback = score_goal.feedback(body.get("feedback", "none"))
    except ValueError as exc:
        return json_error(400, str(exc))
    if feedback != "none" and not session.sdef.score_goal:
        return json_error(409, "score goal is not enabled for this session")
    if request_id is not None:
        prior = session_input.latest(session.sdef.name, request_id)
        if prior and (prior.get("status") == "sent"
                      or session_input.delivering(session.sdef.name, request_id)):
            return json_response(_keys_payload(session, 0, duplicate=True))
    if request_id is not None and (
        session.exited or getattr(session, "held_at", None)
        or session_input.pending(session.sdef.name)
        or session_input.flushing(session.sdef.name)
    ):
        return _queue_session_input(request, session, body, request_id,
                                    feedback)
    paste = body.get("paste")
    if paste is not None:
        if not isinstance(paste, str):
            return json_error(400, "'paste' must be a string")
        if getattr(session, "held_at", None):
            session = await request.app["manager"].wake_async(session.sdef.name, by="input")
            await session.await_input_ready()
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
                return json_response(_keys_payload(session, 0, duplicate=True))
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
        _apply_score_feedback(request, session, feedback)
        return json_response(_keys_payload(session, len(data)))
    keys = body.get("keys")
    if not isinstance(keys, list) or not all(isinstance(k, str) for k in keys):
        return json_error(400, "'keys' must be a list of strings")
    if getattr(session, "held_at", None):
        session = await request.app["manager"].wake_async(session.sdef.name, by="input")
        await session.await_input_ready()
    audit_text = keys[0] if request_id and len(keys) == 2 and keys[1] == "Enter" else None
    if request_id is not None:
        if audit_text is None:
            return json_error(400, "'input_id' requires [text, 'Enter'] keys")
        prior = session_input.latest(session.sdef.name, request_id)
        if prior and prior.get("status") == "sent":
            return json_response(_keys_payload(session, 0, duplicate=True))
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
    _apply_score_feedback(request, session, feedback)
    return json_response(_keys_payload(session, len(data)))


def _queue_session_input(request, session, body: dict, request_id: str,
                         feedback: str) -> web.Response:
    """The session-line send for a session that has exited: record the line
    as ``queued`` in its input journal and answer 202. The manager types it
    when the session is launched again (``session_input.flush``).

    Only the operator's session line queues (it always carries ``input_id``);
    a bare keys call to an exited session still fails, because a caller with
    no durable id has nothing to see the queued line by.
    """
    name = session.sdef.name
    if getattr(session, "archived_at", None):
        return json_error(
            409, f"session {name!r} is archived — nothing was queued; "
                 f"resume it first")
    if feedback != "none":
        return json_error(
            409, "a reward/penalty point cannot ride a queued line — it applies "
                 "only to input that lands; nothing was queued")
    paste = body.get("paste")
    if paste is not None:
        if not isinstance(paste, str):
            return json_error(400, "'paste' must be a string")
        text = paste
    else:
        keys = body.get("keys")
        if not (isinstance(keys, list) and len(keys) == 2
                and isinstance(keys[0], str) and keys[1] == "Enter"):
            return json_error(400, "'input_id' requires [text, 'Enter'] keys")
        text = keys[0]
    prior = session_input.latest(name, request_id)
    if prior:
        if prior.get("status") == "queued" and getattr(session, "held_at", None):
            request.app["manager"].wake_soon(name, by="input")
        return json_response({"ok": True, "bytes": 0, "duplicate": True,
                              "status": prior.get("status")})
    session_input.queue(name, request_id=request_id, text=text)
    waiting = session_input.pending(name)
    # A respawn can land between reading the record and writing the line:
    # the new incarnation's launch-time flush found nothing, so start one.
    manager = request.app["manager"]
    try:
        current = manager.get(name)
    except Exception:  # noqa: BLE001 — cleared meanwhile; stays queued
        current = None
    if current is not None and getattr(current, "held_at", None):
        manager.wake_soon(name, by="input")
    elif current is not None and not current.exited:
        manager.flush_queued_input(current)
    return json_response({
        "ok": True, "bytes": 0, "queued": True, "input_id": request_id,
        "position": len(waiting),
    }, status=202)


async def h_session_input_journal(request: web.Request) -> web.Response:
    """Recent durable submissions made through the session-line control.

    ``entries`` is the raw event log; ``requests`` folds it to one row per
    submitted line with its current status (queued / accepted / sent /
    failed / cancelled), which is what the panel shows.
    """
    session = _session(request)
    try:
        limit = int(request.query.get("limit", "50"))
    except ValueError:
        return json_error(400, "'limit' must be an integer")
    name = session.sdef.name
    return json_response({
        "session": name,
        "entries": session_input.read(name, limit=limit),
        "requests": session_input.requests(name, limit=limit),
        "queued": len(session_input.pending(name)),
    })


async def h_session_input_cancel(request: web.Request) -> web.Response:
    """Withdraw a line that is still queued for an exited session."""
    session = _session(request)
    name = session.sdef.name
    request_id = request.match_info["request_id"]
    entry = session_input.cancel(name, request_id)
    if entry is None:
        prior = session_input.latest(name, request_id)
        if prior is None:
            return json_error(404, f"no input {request_id!r} for {name!r}")
        return json_error(
            409, f"input {request_id!r} is {prior.get('status')!r}, not "
                 f"queued — only a queued line can be withdrawn")
    return json_response({"ok": True, "entry": entry})


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
        if cflow_clock.is_held(session):
            # A held session wakes for it and takes it once back (the CLI's
            # cflow nudges, claunch-2xyjw); acceptance is not a receipt here
            # either.
            queued = request.app["manager"].wake_soon(
                session.sdef.name, by="deliver", deliver=text)
            return json_response({"ok": True, "queued": queued, "delivered": False,
                                  "woken": queued})
        queued = session.queue_delivery(text)
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
        child = manager.stage_child(
            parent, body, warnings=warnings, exempt_depth=True,
            created_by=actors.with_reason(actors.from_request(request), "pr-monitor"),
        )
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
    try:
        await asyncio.to_thread(session_prs.record, name, result, body)
        # An exited row is kept and its files looked at every CHECK_TTL
        # seconds (claunch-lcwv3): this record shows on the next poll.
        exited_rows.expire(name)
    except OSError as exc:
        warnings.append(f"PR record: {exc}")
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
        if session.exited and not getattr(session, "held_at", None):
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


async def h_session_line_sync(request: web.Request) -> web.Response:
    """The session line's shared state (:mod:`line_sync`).

    GET answers the state. POST changes it: ``mode`` (``local`` /
    ``remote``) switches the session -- every viewer is moved with it -- and
    ``text`` (with ``if_text`` for a conditional write) replaces the shared
    text, which is refused with 409 while the session is ``local``. ``client``
    is the writing page's own id, echoed back so it can tell its write from
    somebody else's. Either change goes out to every attached viewer as a
    ``line_sync`` frame; ``viewers`` says how many."""
    session = _session(request)
    name = session.sdef.name
    if request.method == "GET":
        return json_response(line_sync.get(name).frame())
    body = await _json_body(request)
    client = str(body.get("client") or "")[:64]
    mode = body.get("mode")
    has_text = "text" in body
    if mode is None and not has_text:
        return json_error(400, "give 'mode' or 'text'")
    changed = False
    try:
        if mode is not None:
            _, moved = line_sync.set_mode(name, mode, client=client)
            changed = changed or moved
        if has_text:
            if_text = body.get("if_text")
            if if_text is not None and not isinstance(if_text, str):
                return json_error(400, "'if_text' must be a string")
            _, wrote = line_sync.set_text(
                name, body.get("text"), client=client, if_text=if_text
            )
            changed = changed or wrote
    except line_sync.LineSyncError as exc:
        status = 409 if isinstance(exc, line_sync.NotRemote) else 400
        return json_error(status, str(exc))
    state = line_sync.get(name)
    viewers = line_sync.publish(session, state) if changed else 0
    return json_response({**state.frame(), "changed": changed, "viewers": viewers})


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


async def h_session_retrospective(request: web.Request) -> web.Response:
    """The retrospective pack, for a session that is already running.

    An on-demand prompt pack (prompt_packs.on_demand_block): the copy the
    session's workspace installed, else claunch's. GET returns the text and
    touches nothing -- a run's last node has the agent pull it with
    ``claunch retro --print``. POST types it into the session's terminal, the
    person's door for a session whose run did not end normally. ``?pack=``
    names another on-demand pack; an opening pack is refused (400).
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    session = manager.get(name)
    pack_id = (request.query.get("pack") or prompt_packs.RETROSPECTIVE).strip()
    owner = workspaces.owning(str(session.sdef.cwd))
    try:
        block = await asyncio.to_thread(
            prompt_packs.on_demand_block,
            Path(owner.path) if owner is not None else None,
            pack_id,
        )
    except prompt_packs.PackError as exc:
        return json_error(400, str(exc))
    if request.method == "GET":
        return json_response({"session": name, "pack": pack_id, "block": block})
    delivered = await session.deliver(block)
    return json_response(
        {"ok": True, "session": name, "pack": pack_id, "delivered": delivered}
    )


async def h_session_conversation(request: web.Request) -> web.Response:
    """Record the conversation id the harness says this session is on.

    Body: ``{"conversation_id": ..., "source": ..., "transcript_path": ...}``.
    ``conversation_id`` is required; the rest is carried into the log line.
    The caller is the session's own SessionStart hook (``claunch rebrief``
    reads claude's hook payload off stdin and posts it here), and the moment
    it matters is ``/clear``: claude continues in a new conversation under a
    new id, and until the daemon hears of it a restore reopens the old one and
    the transcript page reads it. Reporting the id already pinned is a no-op
    (``/compact`` keeps the id), so the hook can post unconditionally.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    try:
        body = await request.json()
    except Exception:
        body = {}
    if not isinstance(body, dict):
        body = {}
    conversation_id = str(body.get("conversation_id") or "").strip()
    if not conversation_id:
        return json_error(400, "'conversation_id' is required")
    try:
        outcome = manager.repin_conversation(
            name, conversation_id, source=str(body.get("source") or ""),
        )
    except ManagerError as exc:
        return json_error(404, str(exc))
    return json_response({"ok": True, "session": name, **outcome})


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
    fails. ``?refresh=1`` bypasses the cache. ``?cached=1`` reads the last
    saved result regardless of changed evidence, composing only if absent.
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    try:
        session = manager.get(name)
    except ManagerError:
        return json_error(404, f"no session named {name!r}")
    refresh = request.query.get("refresh") in ("1", "true")
    if not refresh and request.query.get("cached") in ("1", "true"):
        saved = briefing.cached_result(name)
        if saved is not None:
            return json_response(saved)
    cfg = briefing.llm_config()
    if not briefing.llm_configured(cfg):
        return json_error(400, "llm not configured")
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
    """Ask an active agent to read current checks and report fresh values.

    A held session is asleep, not gone: the request wakes it and the refresh
    block is queued into the woken session (``woken: true``, claunch-t1r9j).
    """
    manager: SessionManager = request.app["manager"]
    name = request.match_info["name"]
    try:
        session = manager.get(name)
    except ManagerError:
        return json_error(404, f"no session named {name!r}")
    held = bool(session.exited and getattr(session, "held_at", None))
    if session.exited and not held:
        return json_error(409, f"session {name!r} has exited")
    try:
        checks = status_checks.session_entries(name, enabled_only=True)
    except status_checks.StatusCheckError as exc:
        return json_error(500, str(exc))
    if not checks:
        return json_response({"session": name, "delivered": False, "checks": []})
    if held:
        delivered = manager.wake_soon(
            name, by="status-checks", deliver=status_checks.REFRESH_PROMPT)
        return json_response({"session": name, "delivered": delivered,
                              "woken": delivered, "checks": checks})
    delivered = await session.deliver(status_checks.REFRESH_PROMPT)
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


async def h_session_transcript_info(request: web.Request) -> web.Response:
    """Where one session's conversation is, and how big it is.

    The question "which file is this session's conversation" on its own,
    without building the index a page or a search would. Deep by default,
    unlike the fleet listing: this resolves one session, and the walk it may
    cost is one walk.
    """
    session = _session(request)
    row = await asyncio.to_thread(
        transcript_view.info,
        session.sdef.name,
        session.sdef,
        deep=request.query.get("deep") not in ("0", "false"),
    )
    return json_response(row)


async def h_session_transcript_search(request: web.Request) -> web.Response:
    """Where in this session's conversation a string or pattern was said.

    The page answers "what is in front of me"; this answers "when did X come
    up", which is the question a person asks of a conversation they did not
    watch. ``q`` is a literal unless ``regex=1``; ``role``, ``prose`` and
    ``case`` narrow it. Run in a thread for the same reason the page is: the
    first search of a large transcript builds the index, and the event loop
    has terminals to pump.
    """
    session = _session(request)
    query = request.query.get("q") or ""
    if not query:
        return json_error(400, "'q' is required")
    try:
        limit = int(
            request.query.get("limit", transcript_view.SEARCH_LIMIT_DEFAULT)
        )
    except ValueError:
        return json_error(400, "'limit' must be an integer")
    roles = [r for r in (request.query.get("role") or "").split(",") if r]
    try:
        view = await asyncio.to_thread(
            transcript_view.search,
            session.sdef.name,
            session.sdef,
            query=query,
            regex=request.query.get("regex") in ("1", "true"),
            ignore_case=request.query.get("case") not in ("1", "true"),
            limit=limit,
            roles=roles or None,
            prose_only=request.query.get("prose") in ("1", "true"),
        )
    except ValueError as exc:
        return json_error(400, str(exc))
    return json_response(view)


async def h_transcripts_list(request: web.Request) -> web.Response:
    """Which sessions have a conversation on disk, and where each one is.

    The fleet's index of readable conversations: one row per session that
    has a transcript, newest written first, so a reader looking for where
    something was said starts with the sessions that said something
    recently. ``all=1`` keeps the sessions with nothing to read as well.

    The lookup is the shallow one by default (see
    :func:`transcript_view.source_of`): the deep searches cost a directory
    walk per session, and this endpoint resolves hundreds at once. ``deep=1``
    asks for them anyway, for a reader who knows a session's file is filed
    under a slug we spell differently.
    """
    manager: SessionManager = request.app["manager"]
    state = request.query.get("state") or "all"
    states = {
        "all": None,
        "active": session_mod.CATEGORY_RUNNING,
        "killed": session_mod.CATEGORY_KILLED,
        "paused": session_mod.CATEGORY_PAUSED,
        "hold": session_mod.CATEGORY_HOLD,
        "delayed": session_mod.CATEGORY_DELAYED,
        "failed": session_mod.CATEGORY_FAILED,
        "archived": session_mod.CATEGORY_ARCHIVED,
    }
    if state not in states:
        return json_error(400, f"invalid session list state: {state!r}")
    want = states[state]
    deep = request.query.get("deep") in ("1", "true")
    keep_empty = request.query.get("all") in ("1", "true")

    listed = []
    for s in manager.list():
        category = session_mod.session_category(s)
        if want is not None and category != want:
            continue
        listed.append((s.sdef.name, s.sdef, category, s.status()))

    def resolve():
        out = []
        for name, sdef, category, status in listed:
            row = transcript_view.info(name, sdef, deep=deep)
            row["status"] = status
            row["category"] = category
            if row["source"] or keep_empty:
                out.append(row)
        # Newest conversation first; the ones with no file (``all=1``) sort
        # last, having no time to be ordered by.
        out.sort(key=lambda r: r.get("modified_at") or "", reverse=True)
        return out

    rows = await asyncio.to_thread(resolve)
    return json_response({"sessions": rows, "total": len(rows)})


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


def _beads_extra_roots(request: web.Request) -> list:
    """The directories a Beads view should show a board for beyond the
    sessions' own.

    The daemon's own directory has always been one, so the page is never
    empty. Every registered **workspace** is one now as well: a workspace's
    board is its own (:mod:`claude_launcher.beads_db`), and a board nobody
    happens to have a session in is exactly the one the operator is about to
    file the first issue on. Without them the create form could offer only
    the boards the fleet was already standing in, which is how every issue
    ended up on the daemon's.

    ``?cwd=`` goes first when it is given, because it is the board the caller
    asked about and the form uses the first entry as its default.
    """
    roots = [os.getcwd()]
    for ws in workspaces.list_all():
        if ws.exists():
            roots.append(ws.path)
    cwd = request.query.get("cwd")
    if cwd:
        roots.insert(0, cwd)
    return roots


async def h_beads_fleet(request: web.Request) -> web.Response:
    """Every board the fleet touches — the Beads page.

    One entry per board: every registered workspace's, plus the boards the
    sessions' own directories resolve to and the daemon's own. Each issue is
    tagged with the sessions it belongs to and why, so the page can draw the
    session↔issue match the rail draws per session, for everybody at once.
    """
    manager: SessionManager = request.app["manager"]
    extra = _beads_extra_roots(request)
    view = await request.app["beads"].fleet_view(list(manager.list()), extra)
    return json_response(view)


async def h_beads_stream(request: web.Request) -> web.Response:
    """One numbered Beads page.

    ``status`` may be repeated and narrows the page in the board's database,
    so the page the reader is on is a page of what is being drawn rather than
    fifty rows of which some happen to match. Omitted, every status is
    included. Each board entry carries ``total``, which is what the page
    control counts pages with.
    """
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
    extra = _beads_extra_roots(request)
    statuses = [s for s in request.query.getall("status", []) if s]
    unknown = [s for s in statuses if s not in beads_mod.KNOWN_STATUSES]
    if unknown:
        return json_error(
            400,
            "unknown status "
            + ", ".join(repr(s) for s in unknown)
            + " (known: "
            + ", ".join(beads_mod.KNOWN_STATUSES)
            + ")",
        )
    view = await request.app["beads"].stream_view(
        list(manager.list()), extra, offset=offset, limit=limit, priority=priority,
        sort=sort, direction=direction, statuses=statuses,
        assignee=request.query.get("assignee", ""),
        board_root=request.query.get("board"),
    )
    return json_response(view)


async def h_beads_boards(request: web.Request) -> web.Response:
    """Where an issue can be filed, and where its session can be opened.

    Two lists the creation form needs before it can be drawn, and neither is
    derivable in the browser: ``boards`` -- one entry per board the fleet can
    file on, the same grouping :func:`h_beads_fleet` uses, each carrying its
    name and database (:func:`daemon.beads.Board.board_head`) and the sessions
    on it so the assignee picker offers names that exist -- and
    ``workspaces``, the registry a recorded workspace is checked against
    (``claunch workspace add <dir>``).

    Cheap on purpose: no issue is listed. A form that had to wait for a
    board listing to draw its own fields would be paying the cost this
    round was about removing.
    """
    manager: SessionManager = request.app["manager"]
    board = request.app["beads"]
    sessions = list(manager.list())
    extra = _beads_extra_roots(request)
    view = await board.boards_view(sessions, extra)
    return json_response({
        **view,
        "default": view["boards"][0]["root"] if view["boards"] else None,
        "workspaces": [w.to_dict() for w in workspaces.list_all()],
    })


async def h_beads_settings(request: web.Request) -> web.Response:
    """Which database every board is — the Settings page's Beads boards card.

    One row per registered workspace plus ``claunch-default``, each carrying
    the database in effect, whether that path was set or derived, whether the
    file is there yet, and the default it would fall back to
    (:func:`claude_launcher.beads_db.listing`). ``suggestions`` lists the
    ``.db`` files already sitting where that board would be, so the field can
    offer them rather than have the operator type a path out.

    ``issues`` is filled per row for a database that exists: a count asked
    of ``br`` (:func:`_beads_issue_count`), so a board that is about to be
    repointed can be seen to have (or not have) work on it before the change
    is made. ``setup`` is :func:`cli_beads.setup_state` -- whether the
    board's ``policy.yaml`` and ``.gitignore`` are what br 0.7 needs, so the
    card can offer to finish a board that exists but was made before them.
    """
    rows = await asyncio.to_thread(
        beads_db.listing, cli_beads.repo_root(os.getcwd())
    )
    board = request.app["beads"]
    await asyncio.gather(*(_beads_fill_row(board, r) for r in rows))
    return json_response({
        "boards": rows,
        "default_board": beads_db.DEFAULT_BOARD,
        "available": request.app["beads"].available(),
    })


async def _beads_fill_row(board, row: dict) -> None:
    """Add what the card shows beyond the listing: ``issues`` and ``setup``."""
    row["issues"] = await _beads_issue_count(board, row)
    row["setup"] = await asyncio.to_thread(
        cli_beads.setup_state, _beads_row_ref(row)
    )


def _beads_row_ref(row: dict) -> beads_db.BoardRef:
    """The board a settings row describes, exactly as the card showed it."""
    return beads_db.BoardRef(
        name=row["board"], db=row["db"], root=row["root"],
        workspace=row["workspace"], configured=row["configured"],
    )


async def _beads_issue_count(board, row: dict) -> Optional[int]:
    """How many issues a board holds, or ``None``.

    Asked of ``br`` -- ``list --all --limit 1`` reports the total -- and not
    read out of the database file. It used to be read with SQLite directly,
    to spare the Settings page a fork per board, but br 0.7's engine keeps
    its own WAL index and treats one a SQLite reader initialised as
    poisoned: every such read left a ``.beads/.br-wal-index-*/`` directory
    behind and made br's next call rebuild the index. Deleted issues
    (tombstones) are not in ``--all`` and are not counted.

    ``None`` for a board whose database is not there, when ``br`` is not
    installed, or when it cannot answer -- the card then says nothing instead
    of claiming zero, which is a different fact from "no issues".
    """
    if not row.get("exists") or not board.available():
        return None
    ref = _beads_row_ref(row)
    try:
        data = await board.br(
            Path(ref.root), ["list", "--all", "--limit", "1"], ref=ref
        )
    except (BeadsError, beads_mod.BeadsUnavailable, OSError):
        return None
    total = data.get("total") if isinstance(data, dict) else None
    return total if isinstance(total, int) else None


async def _beads_board_row(request: web.Request, name: str) -> Optional[dict]:
    """The settings row for one board name, or ``None`` if there is no such
    board. What the two writes below answer with, so a caller sees the state
    it has just put the board in without a second request."""
    rows = await asyncio.to_thread(
        beads_db.listing, cli_beads.repo_root(os.getcwd())
    )
    for row in rows:
        if row["board"] == name:
            await _beads_fill_row(request.app["beads"], row)
            return row
    return None


async def h_beads_settings_set(request: web.Request) -> web.Response:
    """Point one board at a database file, or drop the override.

    Body: ``db``, an **absolute path ending in .db**, or ``null``/``""`` to
    go back to the default (``<workspace>/.beads/beads.db``). The path names
    the file itself rather than a directory holding it, and it is checked
    before it is stored (:func:`claude_launcher.beads_db.check_path`): a
    relative path, a directory, a name with another suffix, or a parent
    directory that is not there are all refused with the reason. A path that
    passes but has no file yet is accepted — the board is created on first
    use, or now with the ``init`` route below.

    404 for a name that is neither ``claunch-default`` nor a registered
    workspace, 400 for a path that cannot be stored.
    """
    name = request.match_info["board"]
    if await _beads_board_row(request, name) is None:
        return json_error(
            404,
            f"no board named {name!r} — boards are the registered workspaces "
            f"plus {beads_db.DEFAULT_BOARD!r}",
        )
    body = await _json_body(request)
    raw = body.get("db")
    if raw is not None and not isinstance(raw, str):
        return json_error(400, "'db' must be a path to a .db file, or null")
    try:
        if raw is None or not raw.strip():
            await asyncio.to_thread(beads_db.clear_db, name)
        else:
            await asyncio.to_thread(beads_db.set_db, name, raw)
    except beads_db.BoardPathError as exc:
        return json_error(400, str(exc))
    except store.StoreError as exc:
        return json_error(500, str(exc))
    # Every directory the daemon had already resolved may now read a
    # different file, so nothing resolved against the old one survives.
    request.app["beads"].forget_paths()
    return json_response({"board": await _beads_board_row(request, name)})


async def h_beads_settings_init(request: web.Request) -> web.Response:
    """Set a board up now, instead of on its first use.

    What ``claunch beads init --workspace <board>`` does
    (:func:`cli_beads.init_board`): the database, with ``br init --prefix
    <board name>`` against the path the board resolves to (or rebuilt from a
    tracked ``issues.jsonl``), so the operator can see the file appear and
    the issue prefix it will mint under before any session is pointed at it;
    then the ``policy.yaml`` and ``.gitignore`` br 0.7 needs beside it. Each
    part already there is left alone — a database that exists is never
    touched; this is not a reset. The answer says which parts were made now
    (``created``, ``imported``, ``policy``, ``gitignore``) and carries the
    row as it stands after.

    404 for a name with no board, 409 for a directory that cannot hold one,
    503 when ``br`` is not installed on this machine.
    """
    name = request.match_info["board"]
    row = await _beads_board_row(request, name)
    if row is None:
        return json_error(
            404,
            f"no board named {name!r} — boards are the registered workspaces "
            f"plus {beads_db.DEFAULT_BOARD!r}",
        )
    board = request.app["beads"]
    # Built from the row rather than resolved again, so the board that is set
    # up is exactly the one the card just showed.
    ref = _beads_row_ref(row)
    try:
        result = await board.init_board(ref)
    except beads_mod.BeadsUnavailable as exc:
        return json_error(503, str(exc))
    except (BeadsError, OSError) as exc:
        return json_error(409, str(exc))
    board.forget_paths()
    result.pop("state", None)
    return json_response({"board": await _beads_board_row(request, name), **result})


async def h_beads_create(request: web.Request) -> web.Response:
    """File an issue on a board — the Beads page's create form.

    Body: ``title`` (required), ``description``, ``priority`` (0..4, or
    ``P2``), ``type``, ``labels`` (a list or a comma-separated string),
    ``assignee``, ``parent``, ``status`` (``open`` or ``in_ready``),
    ``workspace`` (a registered workspace NAME, recorded on the description
    so a session started from this issue opens in the right directory) and
    ``cwd`` (which board; the daemon's own by default).

    Every rule is :func:`daemon.beads.check_new_issue`'s, so the route holds
    none of its own and the refusal text is the same one the tests pin.
    400 for a request the board would not accept, 404 for a directory with
    no board.
    """
    board = request.app["beads"]
    body = await _json_body(request)
    cwd = str(body.get("cwd") or request.query.get("cwd") or os.getcwd())
    root = await board.root_for(cwd)
    if not board.has_board(root):
        return json_error(404, f"no board for {cwd}")
    try:
        spec = beads_mod.check_new_issue(body)
    except beads_mod.BoardRequestError as exc:
        return json_error(400, str(exc))
    try:
        made = await board.create_issue(root, spec)
    except beads_mod.BeadsUnavailable as exc:
        return json_error(503, str(exc))
    except BeadsError as exc:
        return json_error(400, str(exc))
    return json_response(made, status=201)


def _lane_activity(session) -> dict:
    """The status dot's readings for one Queues lane -- the fields the rail's
    dot is graded by (``rail_fields`` in h_sessions_list), read off the session
    directly rather than through ``ctxsize.attach``: attach also reads the
    context window, and the rail runs it in a worker for that reason. Here it
    is one lane head per drawn session, on the loop, so only the four cheap
    readings are taken (``tool_calls_for_session`` is cached on the
    transcript's stat)."""
    out: dict = {
        "last_activity_at": session.last_activity_at(),
        "paused_at": getattr(session, "paused_at", None),
    }
    tracker = getattr(session, "tracker", None)
    if tracker is not None:  # a DeadSession has no screen to have moved
        out["moved_rows"] = tracker.moved_rows(time.monotonic())
    calls = ctxsize.tool_calls_for_session(getattr(session, "sdef", None))
    if calls is not None:
        out["tool_calls"] = calls
    return out


@shared_reads.shared("beads-queues")
async def h_beads_queues(request: web.Request) -> web.Response:
    """Every board's queues, one lane per session — the Beads page's Queues
    tab. Same boards as :func:`h_beads_fleet` (the sessions' directories and
    the daemon's own, or ``?cwd=``); each lane carries the session's issues in
    the order its worker takes them, its status, and the cflow step it is on,
    so the operator sees who is doing what next without opening a terminal.

    ``?fold=1`` answers the rows the tab draws folded as counts, and
    ``?open=spent`` / ``?open=pool`` (repeatable) the folds the reader has
    opened in full -- see :meth:`daemon.beads.Board.queues_view`.
    """
    manager: SessionManager = request.app["manager"]
    extra = _beads_extra_roots(request)
    fold = request.query.get("fold", "") in ("1", "true")
    opened = request.query.getall("open", [])
    # One summarizer per response: every lane's containment check lists the
    # lane's directory, and lanes share directories heavily (this machine's
    # board: 703 sessions over 132 of them). See cflow_clock.run_summarizer.
    view = await request.app["beads"].queues_view(
        list(manager.list()), extra, cflow_for=cflow_clock.run_summarizer(),
        # Only the tab draws lane dots; the rail's pills (no fold) read none.
        activity_for=_lane_activity if fold else None,
        fold=fold, open_folds=opened,
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
    and defaults to the daemon's. An id that board does not have is looked
    for on the fleet's other boards (:meth:`daemon.beads.Board.show_on_any`),
    and the answer's ``root`` names the board it was found on: most links to
    an issue carry no board, and the page asks with the one it is showing.

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
        root, issue = await board.show_on_any(
            root, issue_id, list(request.app["manager"].list()),
            _beads_extra_roots(request),
        )
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
        # The board this issue is on, by name as well as by root: an issue id
        # is unique on its own board and nowhere else, so the panel says which
        # one rather than leaving it to the tab the reader arrived through.
        **board.board_head(root),
        "issue": issue,
        "sessions": linked,
        "reports": reports_mod.for_issue(issue_id),
        # Derived here rather than re-parsed in the page: the front matter's
        # spelling is the daemon's (beads_meta), and a second reader of it in
        # JavaScript is a second place for it to drift.
        "workspace": beads_meta.workspace_of(issue),
        # Whether that name is STILL registered. It was checked when it was
        # written (Board.set_workspace refuses an unregistered one), but a
        # workspace can be removed afterwards and the issue keeps naming it.
        # Said here because the page cannot tell the two apart otherwise: a
        # name it cannot resolve looks exactly like no name at all, and the
        # creation modal would open on its own default while the board went
        # on claiming the issue's directory had been honoured.
        "workspace_known": bool(
            beads_meta.workspace_of(issue)
            and workspaces.get(beads_meta.workspace_of(issue)) is not None
        ),
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


async def _search_args(request: web.Request):
    """The query string a search reads, validated the same way for the JSON
    answer and the stream. ``(args, error_response)``."""
    service: rag_mod.RagService = request.app["rag"]
    kind = (request.query.get("kind") or "beads").strip()
    if kind not in rag_mod.KINDS:
        return None, json_error(400, f"kind must be one of {', '.join(rag_mod.KINDS)}")
    mode = request.query.get("mode", "general")
    if mode not in ("general", "active") or (mode == "active" and kind != "all"):
        return None, json_error(400, "mode must be general, or active with kind=all")
    query = (request.query.get("q") or "").strip()
    if not query:
        return None, json_error(400, "q is required")
    if not service.configured():
        return None, json_error(400, "rag: block not configured (base_url, api_key, embedding_model)")
    limit = _int_query(request, "limit", 10, 1, 50)
    wait = _int_query(request, "wait", 2, 0, 30)
    if limit is None or wait is None:
        return None, json_error(400, "limit must be 1..50 and wait 0..30")
    rerank = (request.query.get("rerank") or "1") not in ("0", "false", "no")
    root, err = await _search_root(request, kind)
    if err is not None:
        return None, err
    return SimpleNamespace(
        service=service, kind=kind, query=query, root=root,
        limit=limit, rerank=rerank, wait=float(wait), mode=mode,
    ), None


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
    args, err = await _search_args(request)
    if err is not None:
        return err
    try:
        view = await args.service.search(
            args.kind, args.query, root=args.root, limit=args.limit,
            rerank=args.rerank, wait=args.wait, mode=args.mode,
        )
    except rag_mod.RagError as exc:
        return json_error(502, str(exc))
    return json_response(view)


async def h_search_stream(request: web.Request) -> web.StreamResponse:
    """The same search as ``GET /api/search``, answered as server-sent events.

    ``event: ranked`` carries the vector ranking as soon as it exists (tens
    of milliseconds on the zvec index); ``event: reranked`` follows with the
    reranker's order, or ``event: error`` when the reranker fails -- the
    ranked answer already went out, so a page keeps showing it. ``ranked``
    says in ``rerank_pending`` whether a second event is coming. Each event's
    ``data`` is one JSON object shaped like the ``/api/search`` answer.

    Measured on the live daemon before this existed: 43-104 ms without the
    reranker, 1.2-11 s with it, top-10 overlap 8-10 of 10 (claunch-1sszr).
    One request instead of two lets the query embedding be computed once.

    Failures before the first event answer as JSON (400/502) with the codes
    ``/api/search`` uses, so a reader checks the status once. A client that
    goes away closes the generator, which abandons the reranker call.
    """
    args, err = await _search_args(request)
    if err is not None:
        return err
    stream = args.service.search_stream(
        args.kind, args.query, root=args.root, limit=args.limit,
        rerank=args.rerank, wait=args.wait, mode=args.mode,
    )
    async with contextlib.aclosing(stream):
        try:
            event, view = await stream.__anext__()
        except rag_mod.RagError as exc:
            return json_error(502, str(exc))
        except ValueError as exc:
            return json_error(400, str(exc))
        resp = web.StreamResponse(headers={
            "Content-Type": "text/event-stream; charset=utf-8",
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        })
        await resp.prepare(request)
        try:
            while True:
                await resp.write(_sse(event, view))
                try:
                    event, view = await stream.__anext__()
                except StopAsyncIteration:
                    break
        except (ConnectionResetError, asyncio.CancelledError):
            raise
        except Exception as exc:  # noqa: BLE001 -- the stream already started
            log.exception("search stream failed after it started")
            with contextlib.suppress(ConnectionResetError):
                await resp.write(_sse("error", {"error": str(exc)}))
        with contextlib.suppress(ConnectionResetError):
            await resp.write_eof()
        return resp


def _sse(event: str, payload) -> bytes:
    """One server-sent event. ``_dumps`` writes no newlines, so ``data`` is
    one line and needs no splitting."""
    return f"event: {event}\ndata: {_dumps(payload)}\n\n".encode("utf-8")


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

    ``?cwd=`` narrows it to one directory -- what the new-session form asks
    for an input declared ``precheck: gh`` (improv-worker-v2's force_pr)
    before the user may turn it on.
    """
    cwd = str(request.query.get("cwd") or "").strip()
    if cwd:
        path = cflow_state.resolve_cwd(cwd)
        repos = [(Path(path).name or path, path)]
    else:
        repos = None
    return json_response(
        await asyncio.to_thread(
            lambda: ghcli.status(repos if repos is not None else ghcli.daemon_repositories()))
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
    """File an issue for a session from its detail panel and assign it there
    and then (claunch-4g76d). Body: ``title`` (required), ``description``
    (optional; added under the title in the goal section), ``priority``
    (0..4 or ``P2``; 2 by default), ``notify`` (default true).

    The creator is the person (:data:`daemon.beads.USER_ACTOR`), the assignee
    the session, the status ``open`` -- the listing the worker's queue steps
    read. A session with no issue yet takes it as its own (linked the way a
    creation-time one is); a session that has one gets it on its queue. A
    running session is also told, with a block typed into it, so the loop
    picks it up without waiting for its next board read. 409 for an exited
    session: nothing would ever take the work up.
    """
    manager: SessionManager = request.app["manager"]
    session = manager.get(request.match_info["name"])
    body = await _json_body(request)
    try:
        spec = beads_mod.check_new_issue(
            {"title": body.get("title"), "priority": body.get("priority", 2)}
        )
    except beads_mod.BoardRequestError as exc:
        return json_error(400, str(exc))
    if session.exited and not getattr(session, "held_at", None):
        return json_error(
            409, f"{session.sdef.name} has exited -- file it on the board "
            "and queue it to a running session instead"
        )
    board = request.app["beads"]
    try:
        made = await board.create_for(
            session, title=spec["title"],
            description=str(body.get("description") or ""),
            priority=spec["priority"],
        )
    except beads_mod.BeadsUnavailable as exc:
        return json_error(503, str(exc))
    except BeadsError as exc:
        return json_error(400, str(exc))
    primary = not session.sdef.issue
    if primary:
        beads_mod.link_issue(session, made["issue"])
        manager.persist()
    notified = False
    if body.get("notify", True) is not False:
        notified = session.queue_delivery(beads_mod.compose_filed_notice(
            session.sdef.name, made["issue"], spec["title"],
            priority=spec["priority"], primary=primary,
        ))
    return json_response(
        {**made, "primary": primary, "notified": bool(notified),
         "beads": await board.session_view(session)},
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
