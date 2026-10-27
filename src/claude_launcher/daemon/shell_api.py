"""A session's own shell: its output, and the input its agent proposes.

Every session has one shell (``daemon/session_shells.py``, shown in the
session's ``cli`` split pane). The shell runs with the user's privileges, so
an agent's share of it is deliberately two-faced: it may **read** what the
shell printed, and it may **propose** input the user can accept in that
pane. Nothing an agent sends reaches the child except through ``accept``,
which is the user's control, and the route refuses an accept that comes in
shaped like a session's call.

The address is the session: there is no route that reaches a shell by any
other name, which is the whole permission boundary between sessions. A
proposal is owned by the session whose shell it was made in — ``session`` in
the body must be the path's name — so "propose to somebody else's shell" is
not an operation that exists.

  GET  /api/sessions/{name}/shell                     status and read cursor
  GET  /api/sessions/{name}/shell/output?offset=&limit=  output since a cursor
  GET  /api/sessions/{name}/shell/suggestions?state=  the proposal queue
  POST /api/sessions/{name}/shell/suggestions         propose {session, text, why?}
  POST /api/sessions/{name}/shell/suggestions/{id}/accept   user: type and run
  POST /api/sessions/{name}/shell/suggestions/{id}/reject   user: decline
  POST /api/sessions/{name}/shell/suggestions/{id}/withdraw proposer: take back

Every route answers 404 when the session is unknown. Reading a session that
never had a shell is a 404 too — a reader does not conjure a PTY, the
``cli`` pane's viewer does (``ws.session_shell_ws``). ``suggest`` is the one
exception: it creates the session's shell record (not its child) so an agent
can propose before anyone has looked at the pane, which is the order this
feature is normally used in.

Reading is not rate-limited and carries no side effect; a reader that fell
behind the ring is told how many bytes it missed (``dropped``) rather than
being handed a silent gap (``read_output``).
"""

from __future__ import annotations

import logging
from typing import Optional

from aiohttp import web

log = logging.getLogger(__name__)


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


def _known_session(app, name: str) -> bool:
    """Whether ``name`` is a session this daemon knows (live or held)."""
    try:
        app["manager"].get(name)
        return True
    except Exception:  # noqa: BLE001 -- no such session
        return False


def install(app) -> None:
    shells = app["session_shells"]
    manager = app["manager"]

    def _named(request) -> str:
        """The session the path addresses, refusing one this daemon lacks."""
        name = request.match_info["name"]
        if not _known_session(app, name):
            raise web.HTTPNotFound(
                text='{"error": "no such session"}', content_type="application/json"
            )
        return name

    def _shell(request):
        """The addressed session's existing shell, or a 404."""
        name = _named(request)
        shell = shells.get(name)
        if shell is None:
            raise web.HTTPNotFound(
                text='{"error": "this session has no shell yet"}',
                content_type="application/json",
            )
        return name, shell

    def status(name: str, shell) -> dict:
        pending = shell.suggestions("pending")
        return {
            "id": name,
            "pid": shell.pid,
            "exited": bool(shell.exited),
            "exit_code": shell.exit_code,
            "cols": shell.cols,
            "rows": shell.rows,
            "offset": shell.total,
            "pending": len(pending),
        }

    def _own_session(body: dict, name: str) -> str:
        """The proposing session: the path's session, and nobody else's.

        An agent proposes to the shell of the session it is in; the MCP tool
        and ``claunch shell`` both send ``$CLAUNCH_SESSION``. A body naming a
        different session is refused rather than honoured — that is the
        cross-session boundary, stated once, here.
        """
        session = _session_of(body)
        if session is None:
            raise web.HTTPForbidden(
                text='{"error": "this act belongs to the session whose shell it is (send session)"}',
                content_type="application/json",
            )
        if session != name:
            raise web.HTTPForbidden(
                text='{"error": "a proposal belongs to the session whose shell it is"}',
                content_type="application/json",
            )
        return session

    async def h_status(request):
        name, shell = _shell(request)
        return web.json_response(status(name, shell))

    async def h_output(request):
        _name, shell = _shell(request)
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
        _name, shell = _shell(request)
        state = request.query.get("state")
        return web.json_response({"suggestions": shell.suggestions(state or None)})

    async def h_suggest(request):
        name = _named(request)
        body = await json_body(request)
        session = _own_session(body, name)
        # The queue is the daemon's memory, not the child's: proposing before
        # anyone opened the pane is the normal order, so this creates the
        # record. The child still starts with the pane's first viewer.
        shell = shells.get_or_create(name)
        try:
            row = shell.suggest(session, str(body.get("text") or ""),
                                str(body.get("why") or ""))
        except ValueError as exc:
            return web.json_response({"error": str(exc)}, status=400)
        return web.json_response({"suggestion": row}, status=201)

    async def h_accept(request):
        _name, shell = _shell(request)
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
        _name, shell = _shell(request)
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
        name, shell = _shell(request)
        body = await json_body(request)
        session = _own_session(body, name)
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

    base = "/api/sessions/{name}/shell"
    r = app.router
    r.add_get(base, h_status)
    r.add_get(base + "/output", h_output)
    r.add_get(base + "/suggestions", h_suggestions)
    r.add_post(base + "/suggestions", h_suggest)
    r.add_post(base + "/suggestions/{sid}/accept", h_accept)
    r.add_post(base + "/suggestions/{sid}/reject", h_reject)
    r.add_post(base + "/suggestions/{sid}/withdraw", h_withdraw)
