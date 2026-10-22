"""Shared user shell routes: the CLI tab's shell, opened to agents.

The shell itself is :class:`claude_launcher.daemon.clipty.ShellPty` -- one
persistent, unmanaged shell with the user's own privileges. This module is
its HTTP face for everyone who is not the terminal: agent sessions may read
what the shell printed, and may *propose* input the user can accept. Nothing
an agent sends through these routes reaches the child except through
``accept``, which is the user's control; a body that names ``session`` acts
as that session (the ``shell_*`` MCP tools always send
``$CLAUNCH_SESSION``), and a body without one acts as the user (the
dashboard), the daemon's usual trust model.

  GET  /api/shell                                 status and read cursor
  GET  /api/shell/output?offset=&limit=           output since a cursor
  GET  /api/shell/suggestions?state=              the proposal queue
  POST /api/shell/suggestions                     propose {session, text, why?}
  POST /api/shell/suggestions/{id}/accept         user: type it and run it
  POST /api/shell/suggestions/{id}/reject         user: decline it
  POST /api/shell/suggestions/{id}/withdraw       proposer: take it back

Reading is not rate-limited and carries no side effect; a reader that fell
behind the ring is told how many bytes it missed (``dropped``) rather than
being handed a silent gap (``read_output``).
"""

from __future__ import annotations

import logging
from typing import Optional

from aiohttp import web

log = logging.getLogger(__name__)

#: The shell is one per daemon, so its id is fixed rather than generated.
SHELL_ID = "cli"


async def json_body(request) -> dict:
    """The JSON body, or ``{}`` for an empty one.

    Every route here that changes something takes a JSON object; an empty
    body is the dashboard's accept/reject call, which needs no fields.
    """
    try:
        body = await request.json()
    except (ValueError, UnicodeError):
        body = None
    if body is None:
        return {}
    if not isinstance(body, dict):
        raise web.HTTPBadRequest(text='{"error": "body must be a JSON object"}',
                                 content_type="application/json")
    return body


def _session_of(body: dict) -> Optional[str]:
    session = body.get("session")
    return session if isinstance(session, str) and session else None


def _known_session(app, manager, name: str) -> bool:
    """Whether ``name`` is a session this daemon knows (live or held)."""
    try:
        manager.get(name)
        return True
    except Exception:  # noqa: BLE001 -- no such session
        return False


def install(app) -> None:
    shell = app["shell"]
    manager = app["manager"]

    def status() -> dict:
        pending = shell.suggestions("pending")
        return {
            "id": SHELL_ID,
            "pid": shell.pid,
            "exited": bool(shell.exited),
            "exit_code": shell.exit_code,
            "cols": shell.cols,
            "rows": shell.rows,
            "offset": shell.total,
            "pending": len(pending),
        }

    def _required_session(body: dict) -> str:
        session = _session_of(body)
        if session is None:
            raise web.HTTPForbidden(
                text='{"error": "this act belongs to an agent session (send session)"}',
                content_type="application/json",
            )
        if not _known_session(app, manager, session):
            raise web.HTTPNotFound(
                text='{"error": "no such session"}', content_type="application/json"
            )
        return session

    async def h_status(request):
        return web.json_response(status())

    async def h_output(request):
        raw_after = request.query.get("offset")
        raw_limit = request.query.get("limit")
        after: Optional[int] = None
        try:
            if raw_after not in (None, ""):
                after = int(raw_after)
                if after < 0:
                    raise ValueError
            limit = int(raw_limit) if raw_limit not in (None, "") else 64 * 1024
            if limit < 1:
                raise ValueError
        except ValueError:
            return web.json_response(
                {"error": "offset and limit must be non-negative integers"}, status=400
            )
        return web.json_response(shell.read_output(after, limit))

    async def h_suggestions(request):
        state = request.query.get("state")
        return web.json_response({"suggestions": shell.suggestions(state or None)})

    async def h_suggest(request):
        body = await json_body(request)
        session = _required_session(body)
        try:
            row = shell.suggest(session, str(body.get("text") or ""),
                                str(body.get("why") or ""))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"suggestion": row}, status=201)

    async def h_accept(request):
        body = await json_body(request)
        if _session_of(body):
            # The user decides; an agent proposing does not carry the right
            # to type. The MCP surface has no tool for this, and the route
            # refuses a session-shaped call for the same reason.
            return web.json_response(
                {"error": "only the user accepts a shell suggestion"}, status=403
            )
        sid = request.match_info["sid"]
        try:
            row = await shell.accept(sid)
        except KeyError:
            return web.json_response({"error": "no such suggestion"}, status=404)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except RuntimeError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        return web.json_response({"suggestion": row})

    async def h_reject(request):
        body = await json_body(request)
        if _session_of(body):
            return web.json_response(
                {"error": "only the user rejects a shell suggestion"}, status=403
            )
        sid = request.match_info["sid"]
        try:
            row = shell.decide(sid, "rejected", "user")
        except KeyError:
            return web.json_response({"error": "no such suggestion"}, status=404)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        return web.json_response({"suggestion": row})

    async def h_withdraw(request):
        body = await json_body(request)
        session = _required_session(body)
        sid = request.match_info["sid"]
        try:
            row = shell.withdraw(sid, session)
        except KeyError:
            return web.json_response({"error": "no such suggestion"}, status=404)
        except PermissionError as exc:
            return web.json_response({"error": str(exc)}, status=403)
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        return web.json_response({"suggestion": row})

    r = app.router
    r.add_get("/api/shell", h_status)
    r.add_get("/api/shell/output", h_output)
    r.add_get("/api/shell/suggestions", h_suggestions)
    r.add_post("/api/shell/suggestions", h_suggest)
    r.add_post("/api/shell/suggestions/{sid}/accept", h_accept)
    r.add_post("/api/shell/suggestions/{sid}/reject", h_reject)
    r.add_post("/api/shell/suggestions/{sid}/withdraw", h_withdraw)
