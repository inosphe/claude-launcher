"""Dashboard routes for work specs: check, plan and import (``#/spec``).

The same three verbs as ``claunch spec`` (:mod:`claude_launcher.cli_spec`),
over the same code: :mod:`claude_launcher.workspec` validates and plans,
:mod:`claude_launcher.workspec_import` executes. Every body carries the spec
as YAML text (``{"yaml": "..."}``) because the page holds what was pasted or
read from a file, not a path on the daemon's disk.

The board an import writes to is the spec's registered ``workspace``. The
CLI falls back to the current directory, which is the user's checkout; the
daemon's current directory is wherever the shell that started it stood and
it serves every project, so here a spec without a workspace takes one from
the body (``{"workspace": "<registered name>"}``, which the page asks for)
and an import with neither is refused before anything is written. check and
plan answer the directory the name resolves to, so the page can show where
the import will land before it is pressed.

check also answers what the page shows while the text is edited: the YAML
line of each error (``lines``, parallel to ``errors``; null where no line is
known) and, for a valid spec, the sessions and work it describes
(``preview``, :func:`claude_launcher.workspec.preview`). ``GET
/api/spec/template`` hands out :data:`claude_launcher.workspec.TEMPLATE`.

An import runs off the event loop: its board writes are ``claunch beads``
subprocesses and its mesh/session writes go back through this daemon's own
API (the executor's :class:`~claude_launcher.workspec_import.DaemonBackend`),
so the loop has to stay free to answer them.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

from aiohttp import web

from .. import workspec, workspec_import


def _board(doc: dict) -> dict:
    """Where an import of ``doc`` writes: the resolved directory of its
    workspace, or why there is none (no name, or a name that does not
    resolve)."""
    if not doc.get("workspace"):
        return {"board_dir": "", "board_error":
                "this spec names no workspace: choose the registered "
                "workspace whose board the import writes to"}
    try:
        return {"board_dir": workspec_import.board_dir(doc), "board_error": ""}
    except workspec.SpecError as exc:
        return {"board_dir": "", "board_error": str(exc)}


def _registered() -> list:
    from .. import workspaces
    try:
        return [w.name for w in workspaces.list_all()]
    except Exception:  # an unreadable registry is an empty choice, not a 500
        return []


def _summary(doc: dict) -> dict:
    """What the page needs to offer the leader and workspace choices."""
    leader = doc.get("leader") or {}
    mesh = doc.get("mesh") or {}
    return {
        "name": doc.get("name"),
        "title": doc.get("title") or "",
        "mesh": workspec.mesh_name(doc),
        "mesh_mode": mesh.get("mode", "new"),
        "leader_mode": leader.get("mode", "ask"),
        "candidates": [str(c) for c in
                       (leader.get("shared") or {}).get("candidates") or []],
        "workspace": doc.get("workspace") or "",
        "agents": len(workspec._list(doc, "agents")),
        "issues": len(workspec._list(doc, "issues")),
        **_board(doc),
        "workspaces": _registered(),
    }


def _default_backend(doc: dict, board: str) -> workspec_import.Backend:
    from ..daemon_client import ensure_running
    return workspec_import.DaemonBackend(ensure_running(auto_start=False), board)


def install(app, backend: Optional[
        Callable[[dict, str], workspec_import.Backend]] = None):
    """Add the routes. ``backend(doc, board_dir)`` builds the executor's
    backend for a spec (tests pass a fake; the default writes through
    ``claunch beads`` in ``board_dir`` and this daemon's API)."""
    make_backend = backend or _default_backend

    async def body_of(request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            body = None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(
                text='{"error": "body must be a JSON object"}',
                content_type="application/json")
        return body

    def fail(status: int, error: str, **extra) -> web.Response:
        return web.json_response({"error": error, **extra}, status=status)

    def parse(body: dict):
        text = body.get("yaml")
        if not isinstance(text, str) or not text.strip():
            return None, fail(400, "'yaml' must be the spec's text")
        try:
            doc = workspec.load(text)
        except workspec.SpecError as exc:
            errs = [str(exc)]
            return None, fail(400, str(exc), errors=errs,
                              lines=workspec.locate(text, errs))
        errs = workspec.validate(doc)
        if errs:
            return None, fail(400, f"{len(errs)} problem(s) in the spec",
                              errors=errs, lines=workspec.locate(text, errs))
        chosen = body.get("workspace") or ""
        if not isinstance(chosen, str):
            return None, fail(400, "'workspace' must be a registered "
                                   "workspace name", workspace_error=True)
        if chosen:
            named = str(doc.get("workspace") or "")
            if named and named != chosen:
                return None, fail(
                    400, f"the spec names workspace {named!r}; the body's "
                         f"{chosen!r} would contradict it", workspace_error=True)
            # Every action takes it from here: the board, `file`
            # references and the spawned sessions' workspace.
            doc = {**doc, "workspace": chosen}
        return doc, None

    async def check(request):
        doc, bad = parse(await body_of(request))
        if bad:
            return bad
        return web.json_response({"errors": [], "spec": _summary(doc),
                                  "preview": workspec.preview(doc)})

    def planned(doc: dict, body: dict):
        leader = body.get("leader") or ""
        if not isinstance(leader, str):
            return None, fail(400, "'leader' must be own or shared:<session>")
        try:
            return workspec.plan(doc, leader), None
        except workspec.SpecError as exc:
            # The leader choice is the importer's: say so as an answer the
            # page can show beside the choice, not as a broken spec.
            return None, fail(400, str(exc), leader_error=True)

    async def plan(request):
        body = await body_of(request)
        doc, bad = parse(body)
        if bad:
            return bad
        actions, bad = planned(doc, body)
        if bad:
            return bad
        return web.json_response({
            "spec": _summary(doc), "actions": actions,
            "lines": [workspec_import.describe(a) for a in actions],
        })

    async def run_import(request):
        body = await body_of(request)
        doc, bad = parse(body)
        if bad:
            return bad
        _, bad = planned(doc, body)
        if bad:
            return bad
        profile = body.get("profile") or ""
        if not isinstance(profile, str):
            return fail(400, "'profile' must be a profile name")
        board = _board(doc)
        if board["board_error"]:
            return fail(400, board["board_error"], workspace_error=True)

        def work():
            return workspec_import.execute(
                doc, make_backend(doc, board["board_dir"]),
                leader_choice=body.get("leader") or "",
                profile=profile, base_dir=board["board_dir"])

        try:
            log = await asyncio.to_thread(work)
        except workspec.SpecError as exc:
            return fail(400, str(exc))
        except Exception as exc:  # stopped mid-way: what ran is kept
            return fail(409, str(exc), log=getattr(exc, "log", []),
                        resumable=True)
        return web.json_response({"ok": True, "log": log,
                                  "spec": _summary(doc)})

    async def template(_request):
        return web.json_response({"yaml": workspec.TEMPLATE})

    app.router.add_get("/api/spec/template", template)
    app.router.add_post("/api/spec/check", check)
    app.router.add_post("/api/spec/plan", plan)
    app.router.add_post("/api/spec/import", run_import)
