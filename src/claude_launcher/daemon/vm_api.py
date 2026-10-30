"""``/api/vm``: the VM registry's routes (vm-setup-8s6, step 3).

Two kinds of caller share one token, told apart the way the window borrow
routes do it: by the actor a request declares (``daemon/actors.py``). That is
a declaration, not proof, so the boundary that holds is the tool surface:
a session's ``vm_*`` tools call only the session routes below, and the user
routes (register, start, stop, grants, the user taking the screen) refuse a
request that says it comes from a session or a workflow.

User routes:   POST /api/vm, DELETE /api/vm/{id}, POST /api/vm/{id}/{start,
               stop,force_stop,suspend,reconcile,grant,revoke,
               revoke_controller,extra_controller,human}
Anyone:        GET /api/vm, GET /api/vm/{id}
Session:       POST /api/vm/{id}/{acquire,release}  (body ``session`` must be
               the declared actor)
"""

from __future__ import annotations

import asyncio
import json
from typing import Optional

from aiohttp import web

from .. import store
from . import actors, paths, vm_channel, vm_tart, vmhub
from .vmhub import Registry, VmError

_STATUS = {
    "vm_unknown": 404, "tart_missing": 404, "vm_exists": 409, "vm_held": 409,
    "vm_slots_full": 409, "vm_bad_state": 409, "vm_busy_state": 409,
    "vm_not_ready": 409, "vm_busy": 423, "vm_stale": 409, "vm_no_grant": 403,
    "vm_not_controller": 403, "vm_stop_timeout": 504, "vm_boot_timeout": 504,
    "tart_failed": 502, "vm_stop_failed": 502, "vm_unavailable": 503,
    "vm_timeout": 504, "vm_guest_error": 502, "vm_observe_first": 409,
    "vm_bad_scope": 400, "vm_bad_request": 400,
}

#: cua tools that only look. Everything else a session sends through
#: ``/call`` is treated as control: an unknown tool is refused to a session
#: that was only allowed to look, which is the safe way to be wrong.
VIEW_TOOLS = frozenset({
    "get_desktop_state", "get_window_state", "get_screen_size", "list_apps",
    "screenshot",
})
SHOTS_KEEP = 20


def _err(exc: VmError) -> web.Response:
    return web.json_response(exc.as_dict(), status=_STATUS.get(exc.code, 400))


async def _body(request: web.Request) -> dict:
    try:
        body = await request.json()
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise VmError("vm_bad_request", "the body must be JSON") from None
    if not isinstance(body, dict):
        raise VmError("vm_bad_request", "the body must be a JSON object")
    return body


def _hub(request: web.Request) -> tuple:
    return request.app["vm_registry"], request.app["vm_manager"]


def _caller(request: web.Request) -> dict:
    return actors.from_request(request)


def _user_only(request: web.Request) -> Optional[web.Response]:
    if _caller(request).get("kind") in (actors.AGENT, actors.CFLOW):
        return web.json_response(
            {"ok": False, "code": "vm_user_only",
             "error": "vm_user_only: this is the user's to do, from the dashboard or a shell"},
            status=403)
    return None


def _as_session(request: web.Request, body: dict) -> str:
    actor = _caller(request)
    session = str(body.get("session") or "")
    if not session:
        raise VmError("vm_bad_request", "'session' is required")
    if actor.get("kind") == actors.AGENT and actor.get("name") != session:
        raise VmError("vm_no_grant", "a session acts as itself only")
    return session


def _viewable(reg: Registry, session: str):
    return [vm for vm in reg.list() if vm.grants.get(session)]


async def h_list(request: web.Request) -> web.Response:
    reg, _ = _hub(request)
    session = request.query.get("session", "")
    if session:
        rows = [reg.view(vm.id) for vm in _viewable(reg, session)]
        for row in rows:
            # A session sees its own scopes, not the other sessions'.
            row["grants"] = {session: row["grants"].get(session, [])}
    else:
        rows = [reg.view(vm.id) for vm in reg.list()]
    return web.json_response({"vms": rows, "slot_limit": reg.slot_limit})


async def h_get(request: web.Request) -> web.Response:
    reg, _ = _hub(request)
    try:
        return web.json_response(reg.view(request.match_info["id"]))
    except VmError as exc:
        return _err(exc)


async def h_register(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    try:
        body = await _body(request)
        vm_id = str(body.get("id") or "").strip()
        if not vm_id:
            raise VmError("vm_bad_request", "'id' is required")
        vm = reg.add(vm_id, str(body.get("tart_name") or ""),
                     ssh_user=str(body.get("ssh_user") or "admin"),
                     ssh_key=str(body.get("ssh_key") or ""))
        return web.json_response(reg.view(vm.id), status=201)
    except VmError as exc:
        return _err(exc)


async def h_remove(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    try:
        reg.remove(request.match_info["id"])
        return web.json_response({"ok": True})
    except VmError as exc:
        return _err(exc)


def _lifecycle_route(action: str):
    async def handler(request: web.Request) -> web.Response:
        if (refused := _user_only(request)) is not None:
            return refused
        reg, mgr = _hub(request)
        vm_id = request.match_info["id"]
        try:
            reg.get(vm_id)
            channels: vm_channel.Hub = request.app["vm_channels"]
            if action != "start":
                # The channel goes first: it would otherwise reconnect to a
                # guest that is shutting down.
                await channels.drop(vm_id)
            # tart and ssh block: keep them off the event loop.
            await asyncio.to_thread(getattr(mgr, action), vm_id)
            if action == "start":
                channels.ensure(vm_id)
            return web.json_response(reg.view(vm_id))
        except VmError as exc:
            return _err(exc)
    return handler


async def h_reconcile(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    _, mgr = _hub(request)
    try:
        return web.json_response({"changed": await asyncio.to_thread(mgr.reconcile)})
    except VmError as exc:
        return _err(exc)


async def h_grant(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    try:
        body = await _body(request)
        session = str(body.get("session") or "")
        if not session:
            raise VmError("vm_bad_request", "'session' is required")
        got = reg.grant(request.match_info["id"], session, body.get("scopes") or [])
        return web.json_response({"ok": True, "grants": got})
    except VmError as exc:
        return _err(exc)


async def h_revoke(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    try:
        body = await _body(request)
        session = str(body.get("session") or "")
        if not session:
            raise VmError("vm_bad_request", "'session' is required")
        left = reg.revoke(request.match_info["id"], session, body.get("scopes"))
        return web.json_response({"ok": True, "grants": left})
    except VmError as exc:
        return _err(exc)


async def h_revoke_controller(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    try:
        reg.revoke_controller(request.match_info["id"])
        return web.json_response({"ok": True})
    except VmError as exc:
        return _err(exc)


async def h_extra(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    try:
        body = await _body(request)
        reg.add_extra_controller(request.match_info["id"], str(body.get("session") or ""),
                                 str(body.get("reason") or ""))
        return web.json_response({"ok": True})
    except VmError as exc:
        return _err(exc)


async def h_human(request: web.Request) -> web.Response:
    """The user at the screen: ``{"action": "driving" | "idle" | "touch"}``."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    vm_id = request.match_info["id"]
    try:
        action = str((await _body(request)).get("action") or "")
        if action == "driving":
            return web.json_response({"ok": True, **reg.human_driving(vm_id)})
        if action == "idle":
            reg.human_idle(vm_id)
        elif action == "touch":
            reg.human_touch(vm_id)
        else:
            raise VmError("vm_bad_request", "action is driving, idle or touch")
        return web.json_response({"ok": True})
    except VmError as exc:
        return _err(exc)


async def h_acquire(request: web.Request) -> web.Response:
    reg, _ = _hub(request)
    try:
        session = _as_session(request, await _body(request))
        result = reg.acquire(request.match_info["id"], session)
        return web.json_response(result, status=200 if result.get("ok") else 409)
    except VmError as exc:
        return _err(exc)


async def h_release(request: web.Request) -> web.Response:
    reg, _ = _hub(request)
    try:
        session = _as_session(request, await _body(request))
        return web.json_response(reg.release(request.match_info["id"], session))
    except VmError as exc:
        return _err(exc)


def _shots_dir():
    d = paths.daemon_dir() / "vm" / "shots"
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.chmod(0o700)
    except OSError:
        pass
    return d


def _materialize(vm_id: str, result: dict) -> dict:
    """Images in a cua result become files the session can open; the raw
    base64 never travels through a tool answer. The newest
    :data:`SHOTS_KEEP` per VM are kept."""
    import base64
    import time as _time

    content = []
    for block in (result.get("content") or []):
        if isinstance(block, dict) and block.get("type") == "image" and block.get("data"):
            mime = str(block.get("mimeType") or "image/png")
            ext = {"image/jpeg": "jpg", "image/png": "png"}.get(mime, "bin")
            path = _shots_dir() / f"{vm_id}-{int(_time.time() * 1000)}-{len(content)}.{ext}"
            path.write_bytes(base64.b64decode(block["data"]))
            content.append({"type": "image", "path": str(path), "mimeType": mime})
        else:
            content.append(block)
    old = sorted(_shots_dir().glob(f"{vm_id}-*"))[:-SHOTS_KEEP]
    for f in old:
        try:
            f.unlink()
        except OSError:
            pass
    return {**result, "content": content}


async def h_call(request: web.Request) -> web.Response:
    """A session's cua call, through the daemon's one channel."""
    reg, _ = _hub(request)
    vm_id = request.match_info["id"]
    try:
        body = await _body(request)
        session = _as_session(request, body)
        tool = str(body.get("tool") or "")
        if not tool:
            raise VmError("vm_bad_request", "'tool' is required")
        args = body.get("arguments") or {}
        if not isinstance(args, dict):
            raise VmError("vm_bad_request", "'arguments' must be an object")
        scope = vmhub.VIEW if tool in VIEW_TOOLS else vmhub.CONTROL
        reg.begin_call(vm_id, session, scope, body.get("epoch"))
        ok = False
        try:
            channel = request.app["vm_channels"].get(vm_id)
            result = await channel.call(tool, args, float(body.get("timeout") or 30))
            ok = True
        finally:
            reg.end_call(vm_id, session, scope, ok=ok)
        return web.json_response({"ok": True, "scope": scope,
                                  "result": _materialize(vm_id, result)})
    except VmError as exc:
        return _err(exc)


async def h_tools(request: web.Request) -> web.Response:
    reg, _ = _hub(request)
    vm_id = request.match_info["id"]
    try:
        session = _as_session(request, {"session": request.query.get("session", "")})
        reg.begin_call(vm_id, session, vmhub.VIEW)
        try:
            tools = await request.app["vm_channels"].get(vm_id).tools()
        finally:
            reg.end_call(vm_id, session, vmhub.VIEW)
        return web.json_response({"ok": True, "tools": [
            {"name": t.get("name"), "view_only": t.get("name") in VIEW_TOOLS,
             "description": str(t.get("description") or "")[:200]} for t in tools]})
    except VmError as exc:
        return _err(exc)


async def h_exec(request: web.Request) -> web.Response:
    """A shell command in the guest: its own scope, off by default."""
    reg, mgr = _hub(request)
    vm_id = request.match_info["id"]
    try:
        body = await _body(request)
        session = _as_session(request, body)
        command = str(body.get("command") or "")
        if not command.strip():
            raise VmError("vm_bad_request", "'command' is required")
        reg.begin_call(vm_id, session, vmhub.EXEC, body.get("epoch"))
        ok = False
        try:
            out = await asyncio.to_thread(mgr.exec, vm_id, command,
                                          float(body.get("timeout") or 60))
            ok = out["rc"] == 0
        finally:
            reg.end_call(vm_id, session, vmhub.EXEC, ok=ok)
        return web.json_response({"ok": True, **out})
    except VmError as exc:
        return _err(exc)


async def _background(app: web.Application):
    reg: Registry = app["vm_registry"]
    mgr: vm_tart.Manager = app["vm_manager"]

    def _boot():
        reg.load()
        if not reg.list():
            return
        reg.after_restart()
        try:
            mgr.reconcile()
        except VmError:
            # tart is missing or unreadable: the records stay as loaded
            # (stopped) and the next explicit reconcile says why.
            pass

    await asyncio.to_thread(_boot)
    for vm in reg.list():
        if vm.state == "running":
            try:
                await mgr.reprobe(vm.id)
                app["vm_channels"].ensure(vm.id)
            except VmError:
                pass

    async def hand_back():
        while True:
            await asyncio.sleep(2)
            idle = float(store.daemon_config().get("vm_human_idle", 15))
            for vm in reg.list():
                if vm.human == "driving":
                    reg.human_timeout(vm.id, idle)

    task = asyncio.create_task(hand_back())
    yield
    task.cancel()
    await app["vm_channels"].drop_all()


def install(app: web.Application, registry: Optional[Registry] = None,
            manager: Optional[vm_tart.Manager] = None,
            channels: Optional[vm_channel.Hub] = None) -> None:
    cfg = store.daemon_config()
    reg = registry or Registry(paths.vm_json(), slot_limit=int(cfg.get("vm_slot_limit", 2)))
    app["vm_registry"] = reg
    app["vm_manager"] = manager or vm_tart.Manager(
        reg, log_dir=paths.daemon_dir() / "vm",
        graphics=bool(cfg.get("vm_graphics", True)),
        stop_wait=float(cfg.get("vm_stop_wait", 120)),
    )
    app["vm_channels"] = channels or vm_channel.Hub(
        reg, log_dir=paths.daemon_dir() / "vm")
    app.cleanup_ctx.append(_background)
    r = app.router
    r.add_get("/api/vm", h_list)
    r.add_post("/api/vm", h_register)
    r.add_get("/api/vm/{id}", h_get)
    r.add_delete("/api/vm/{id}", h_remove)
    for action in ("start", "stop", "force_stop", "suspend"):
        r.add_post(f"/api/vm/{{id}}/{action}", _lifecycle_route(action))
    r.add_post("/api/vm-reconcile", h_reconcile)
    r.add_post("/api/vm/{id}/grant", h_grant)
    r.add_post("/api/vm/{id}/revoke", h_revoke)
    r.add_post("/api/vm/{id}/revoke_controller", h_revoke_controller)
    r.add_post("/api/vm/{id}/extra_controller", h_extra)
    r.add_post("/api/vm/{id}/human", h_human)
    r.add_post("/api/vm/{id}/acquire", h_acquire)
    r.add_post("/api/vm/{id}/release", h_release)
    r.add_post("/api/vm/{id}/call", h_call)
    r.add_post("/api/vm/{id}/exec", h_exec)
    r.add_get("/api/vm/{id}/tools", h_tools)
