"""Dashboard routes for work specs: check, plan and import (``#/spec``).

The same three verbs as ``claunch spec`` (:mod:`claude_launcher.cli_spec`),
over the same code: :mod:`claude_launcher.workspec` validates and plans,
:mod:`claude_launcher.workspec_import` executes. Every body carries the spec
as YAML text (``{"yaml": "..."}``) because the page holds what was pasted or
read from a file, not a path on the daemon's disk.

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


def _summary(doc: dict) -> dict:
    """What the page needs to offer the leader choice for this spec."""
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
    }


def _default_backend(doc: dict) -> workspec_import.Backend:
    from ..daemon_client import ensure_running
    return workspec_import.DaemonBackend(
        ensure_running(auto_start=False), workspec_import.board_dir(doc))


def install(app, backend: Optional[Callable[[dict], workspec_import.Backend]] = None):
    """Add the routes. ``backend`` builds the executor's backend for a spec
    (tests pass a fake; the default writes through ``claunch beads`` and
    this daemon's API)."""
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
            return None, fail(400, str(exc), errors=[str(exc)])
        errs = workspec.validate(doc)
        if errs:
            return None, fail(400, f"{len(errs)} problem(s) in the spec",
                              errors=errs)
        return doc, None

    async def check(request):
        doc, bad = parse(await body_of(request))
        if bad:
            return bad
        return web.json_response({"errors": [], "spec": _summary(doc)})

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

        def work():
            backend_ = make_backend(doc)
            return workspec_import.execute(
                doc, backend_, leader_choice=body.get("leader") or "",
                profile=profile, base_dir=backend_.board_cwd)

        try:
            log = await asyncio.to_thread(work)
        except workspec.SpecError as exc:
            return fail(400, str(exc))
        except Exception as exc:  # stopped mid-way: what ran is kept
            return fail(409, str(exc), log=getattr(exc, "log", []),
                        resumable=True)
        return web.json_response({"ok": True, "log": log,
                                  "spec": _summary(doc)})

    app.router.add_post("/api/spec/check", check)
    app.router.add_post("/api/spec/plan", plan)
    app.router.add_post("/api/spec/import", run_import)
