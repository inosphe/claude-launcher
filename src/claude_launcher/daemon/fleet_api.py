"""Dashboard routes for fleet presets (``#/fleet``): list, save, delete,
check, expand and launch.

A preset (:mod:`claude_launcher.fleet`) is a reusable tree of agent groups.
A launch expands it into a work spec and imports that spec with the same
executor ``#/spec`` and ``claunch spec import`` use
(:mod:`claude_launcher.workspec_import`), so what the Fleet page previews is
what the import plans.

``POST /api/fleet/expand`` and ``/launch`` take ``{"preset": <name>}`` or
``{"yaml": <preset text>}`` (the editor's unsaved text) plus ``launch``
(see :func:`claude_launcher.fleet.expand`). The daemon fills in what the
page cannot know: whether the named mesh exists (attach instead of create)
and what each group's ``tier`` resolves to today (``model_tiers``).

Launch is a person's action. Its children are created with the profile and
model the person picked for each member (:func:`api.spawn_child_as_person`),
which the spawn policy refuses an agent unless ``spawn.allow_profile`` is
set. So the route refuses an agent caller outright: an agent that wants a
fleet writes a work spec and asks the person to import it.
"""
from __future__ import annotations

import asyncio
from typing import Callable, Optional

import yaml
from aiohttp import web

from .. import fleet, workspec, workspec_import
from . import actors


def dump(doc: dict) -> str:
    """A preset as the daemon writes it: key order kept, unicode as is."""
    return yaml.safe_dump(doc, allow_unicode=True, sort_keys=False, width=100)


def _tiers(tier_id: str):
    from . import model_tiers
    try:
        got = model_tiers.resolve(tier_id)
    except Exception:  # an unreadable tier table resolves nothing
        return None
    if not got:
        return None
    profile, _harness, model = got
    return profile, ("" if model == model_tiers.DEFAULT_MODEL else model)


def _tier_ids() -> list:
    from . import model_tiers
    try:
        block = model_tiers.load()
    except Exception:
        return []
    return [t.get("id") for t in (block or {}).get("tiers") or [] if isinstance(t, dict)]


class FleetBackend(workspec_import.DaemonBackend):
    """The spec executor's backend for a person's launch: a child is created
    in-process with the member's own profile and model
    (:func:`api.spawn_child_as_person`); everything else -- the board, the
    mesh, the top-level root -- goes the :class:`DaemonBackend` way."""

    def __init__(self, client, board_cwd: str, *, app, loop, actor: dict) -> None:
        super().__init__(client, board_cwd)
        self.app, self.loop, self.actor = app, loop, actor

    def spawn(self, parent, body):
        if parent is None:
            return super().spawn(parent, body)
        from . import api
        fut = asyncio.run_coroutine_threadsafe(
            api.spawn_child_as_person(self.app, parent, body, actor=self.actor),
            self.loop)
        status, payload = fut.result()
        if status >= 400:
            raise workspec_import.SpecImportError(
                f"spawn under {parent} refused ({status}): "
                f"{payload.get('error') or payload}")
        return str((payload.get("session") or {}).get("name") or payload.get("name"))


def _default_backend(doc: dict, board: str, *, app, loop, actor) -> workspec_import.Backend:
    from ..daemon_client import ensure_running
    return FleetBackend(ensure_running(auto_start=False), board,
                        app=app, loop=loop, actor=actor)


def install(app, backend: Optional[Callable[..., workspec_import.Backend]] = None):
    """Add the routes. ``backend(doc, board_dir, app=, loop=, actor=)``
    builds the executor's backend (tests pass a fake)."""
    make_backend = backend or _default_backend

    def fail(status: int, error: str, **extra) -> web.Response:
        return web.json_response({"error": error, **extra}, status=status)

    async def body_of(request) -> dict:
        try:
            body = await request.json()
        except (ValueError, UnicodeError):
            body = None
        if not isinstance(body, dict):
            raise web.HTTPBadRequest(text='{"error": "body must be a JSON object"}',
                                     content_type="application/json")
        return body

    def meshes(request) -> set:
        mm = request.app.get("mesh")
        try:
            return {m.name for m in mm.list()} if mm is not None else set()
        except Exception:
            return set()

    def text_of(body: dict) -> str:
        """The preset text a body carries: ``yaml``, or the editor's
        structured ``doc`` dumped the one way the daemon writes presets."""
        if isinstance(body.get("doc"), dict):
            return dump(body["doc"])
        text = body.get("yaml")
        return text if isinstance(text, str) else ""

    def preset_of(body: dict):
        """(doc, error response) for the body's preset: a saved name, or
        the editor's text or document."""
        text = text_of(body)
        try:
            if text.strip():
                doc = fleet.load(text)
            else:
                doc, _ = fleet.get_preset(str(body.get("preset") or ""))
        except fleet.FleetError as exc:
            return None, fail(400, str(exc), errors=[str(exc)])
        errs = fleet.validate(doc)
        if errs:
            return None, fail(400, f"{len(errs)} problem(s) in the preset", errors=errs)
        return doc, None

    def expanded(request, body: dict):
        doc, bad = preset_of(body)
        if bad:
            return None, None, bad
        launch = body.get("launch") or {}
        if not isinstance(launch, dict):
            return None, None, fail(400, "'launch' must be an object")
        mesh = str(launch.get("mesh") or "").strip()
        launch = {**launch, "mesh_exists": bool(mesh) and mesh in meshes(request)}
        try:
            out = fleet.expand(doc, launch, tiers=_tiers)
        except fleet.FleetError as exc:
            return None, None, fail(400, str(exc), launch_error=True)
        return doc, out, None

    async def list_presets(_request):
        rows = await asyncio.to_thread(fleet.list_presets)
        for row in rows:
            if not row["errors"]:
                d = fleet.load(row["yaml"])
                row["groups"] = fleet.groups(d)
                row["shape"] = fleet.shape(d)
        return web.json_response({
            "presets": rows, "default": fleet.DEFAULT_NAME,
            "kinds": list(fleet.KINDS), "issue_modes": list(fleet.ISSUE_MODES),
            "holders": [k for k in workspec.HOLDERS if k != "leader"],
            "max_count": fleet.MAX_COUNT, "max_agents": fleet.MAX_AGENTS,
            "tiers": _tier_ids(), "dir": str(fleet.presets_dir()),
        })

    async def check(request):
        """The editor's live check: 400 only for text that does not parse;
        otherwise the parsed ``doc``, its canonical ``yaml`` and the
        ``errors`` (empty: also the ``groups`` and the members ``shape``
        starts for ``params``)."""
        body = await body_of(request)
        text = text_of(body)
        try:
            doc = fleet.load(text) if text.strip() else \
                fleet.get_preset(str(body.get("preset") or ""))[0]
        except fleet.FleetError as exc:
            return fail(400, str(exc), errors=[str(exc)])
        out = {"doc": doc, "yaml": dump(doc), "errors": fleet.validate(doc)}
        if not out["errors"]:
            try:
                out["groups"] = fleet.groups(doc)
                out["shape"] = fleet.shape(doc, body.get("params") or {})
            except fleet.FleetError as exc:
                out["errors"] = [str(exc)]
        return web.json_response(out)

    async def save(request):
        body = await body_of(request)
        text = text_of(body)
        if not text.strip():
            return fail(400, "'yaml' (or 'doc') must be the preset")
        name = request.match_info["name"]
        try:
            doc = fleet.load(text)
            if doc.get("name") != name:
                return fail(400, f"the preset is named {doc.get('name')!r}, the "
                                 f"address {name!r}", errors=["name: must match the address"])
            row = await asyncio.to_thread(fleet.save_preset, text)
        except fleet.FleetError as exc:
            msg = str(exc)
            errs = [ln[2:] for ln in msg.splitlines() if ln.startswith("- ")] or [msg]
            return fail(400, msg, errors=errs)
        return web.json_response({"ok": True, "preset": row})

    async def delete(request):
        try:
            await asyncio.to_thread(fleet.delete_preset, request.match_info["name"])
        except fleet.FleetError as exc:
            return fail(404, str(exc))
        return web.json_response({"ok": True})

    async def expand(request):
        body = await body_of(request)
        _doc, out, bad = expanded(request, body)
        if bad:
            return bad
        spec = out["spec"]
        try:
            actions = workspec.plan(spec)
        except workspec.SpecError as exc:
            return fail(400, str(exc), launch_error=True)
        board = {"board_dir": "", "board_error": ""}
        if spec.get("workspace"):
            try:
                board["board_dir"] = workspec_import.board_dir(spec)
            except workspec.SpecError as exc:
                board["board_error"] = str(exc)
        else:
            board["board_error"] = "choose the registered workspace the board and sessions use"
        return web.json_response({
            "yaml": out["yaml"], "agents": out["agents"], "name": out["name"],
            "prefix": out["prefix"], "mesh": workspec.mesh_name(spec),
            "mesh_mode": spec["mesh"]["mode"], "preview": workspec.preview(spec),
            "lines": [workspec_import.describe(a) for a in actions], **board,
        })

    async def launch(request):
        actor = actors.from_request(request)
        if actor.get("kind") == actors.AGENT:
            return fail(403, "a fleet launch is a person's: it creates children on "
                             "the profiles the person picked. Write a work spec and "
                             "ask the user to import it")
        body = await body_of(request)
        _doc, out, bad = expanded(request, body)
        if bad:
            return bad
        spec = out["spec"]
        if not spec.get("workspace"):
            return fail(400, "choose the registered workspace the board and sessions use",
                        workspace_error=True)
        try:
            board = workspec_import.board_dir(spec)
        except workspec.SpecError as exc:
            return fail(400, str(exc), workspace_error=True)
        loop = asyncio.get_running_loop()
        default_profile = str((body.get("launch") or {}).get("default_profile") or "")

        def work():
            return workspec_import.execute(
                spec, make_backend(spec, board, app=request.app, loop=loop, actor=actor),
                profile=default_profile, base_dir=board)

        try:
            log = await asyncio.to_thread(work)
        except workspec.SpecError as exc:
            return fail(400, str(exc))
        except Exception as exc:  # stopped mid-way: what ran is kept
            return fail(409, str(exc), log=getattr(exc, "log", []), resumable=True,
                        yaml=out["yaml"], name=out["name"], prefix=out["prefix"])
        return web.json_response({"ok": True, "log": log, "yaml": out["yaml"],
                                  "name": out["name"], "prefix": out["prefix"],
                                  "mesh": workspec.mesh_name(spec)})

    app.router.add_get("/api/fleet/presets", list_presets)
    app.router.add_put("/api/fleet/presets/{name}", save)
    app.router.add_delete("/api/fleet/presets/{name}", delete)
    app.router.add_post("/api/fleet/check", check)
    app.router.add_post("/api/fleet/expand", expand)
    app.router.add_post("/api/fleet/launch", launch)
