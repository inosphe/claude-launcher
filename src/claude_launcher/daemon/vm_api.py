"""``/api/vm``: the VM registry's routes (vm-setup-8s6, step 3).

Two kinds of caller share one token, told apart the way the window borrow
routes do it: by the actor a request declares (``daemon/actors.py``). That is
a declaration, not proof, so the boundary that holds is the tool surface:
a session's ``vm_*`` tools call only the session routes below, and the user
routes (register, start, stop, grants, the user taking the screen) refuse a
request that says it comes from a session or a workflow.

KNOWN LIMIT: the daemon has one token and nothing here can tell a session from
the user once a request leaves out the actor header. A process that can read
the token (any process of the same OS user, a managed session's shell
included) can call a user route as the user: grant itself exec, register a
VM, start or stop one. Granting control already amounts to a shell on the
guest; this limit makes the host's daemon token part of that trust. The same
limit holds for every other route of the daemon (claunch-actor-declared-not-
proven-vs48). What the daemon does about it is record: each user-route change
is written to the audit log with the actor it declared and how it
authenticated (``actor``), so a grant made without a header reads as the
user's, from the CLI. Telling a session's process from the user's (the peer
process of the loopback connection) is a separate follow-up.

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
from pathlib import Path
from typing import Optional

from aiohttp import web

from .. import store
from . import actors, paths, vm_channel, vm_frames, vm_tart, vmhub
from .vmhub import Registry, VmError

_STATUS = {
    "vm_unknown": 404, "tart_missing": 404, "vm_exists": 409, "vm_held": 409,
    "vm_slots_full": 409, "vm_bad_state": 409, "vm_busy_state": 409,
    "vm_not_ready": 409, "vm_busy": 423, "vm_stale": 409, "vm_no_grant": 403,
    "vm_not_controller": 403, "vm_stop_timeout": 504, "vm_boot_timeout": 504,
    "tart_failed": 502, "vm_stop_failed": 502, "vm_unavailable": 503,
    "vm_timeout": 504, "vm_guest_error": 502, "vm_observe_first": 409,
    "vm_bad_scope": 400, "vm_bad_request": 400, "vm_duplicate_unavailable": 501,
}

#: cua tools that only look, from the 62 the guest's cua-driver 0.38.0 lists
#: (⑥, runs of 2026-10-31). A tool not named here is control: an unknown tool
#: is refused to a session that was only allowed to look, which is the safe
#: way to be wrong. clipboard_read and get_browser_state are left out on
#: purpose: what they return is the guest user's own content.
VIEW_TOOLS = frozenset({
    "get_desktop_state", "get_window_state", "get_screen_size", "list_apps",
    "list_windows", "get_accessibility_tree", "get_cursor_position",
    "get_agent_cursor_state", "get_config", "get_guide", "health_report",
    "check_permissions", "verify_state", "zoom", "get_recording_state",
    "get_session", "list_sessions", "get_session_state", "parse_visual_regions",
})

#: cua tools that change the guest beyond what is on its screen -- software
#: installed, the driver's own configuration rewritten, files written to a
#: path the caller names, local files handed to a web page -- and so need the
#: exec grant, as a shell would. run_script is not here: its description says
#: the script runs in a sandbox with no filesystem, network, process or
#: environment, and its only API is the app-driving one that run_steps and
#: the single-action tools have.
EXEC_TOOLS = frozenset({
    "install_extension", "install_ffmpeg", "set_config", "start_recording",
    "browser_download", "browser_set_input_files",
    # Re-invokes whatever tools the trajectory directory on the guest records,
    # by name; the daemon cannot read that directory, so the widest scope.
    "replay_trajectory",
})

#: What run_steps accepts as a step's tool (its own input schema, 0.38.0).
#: A step names its tool as {tool, args} or as {<tool>: args}; a name outside
#: this list is refused here rather than left to the driver to refuse, since a
#: step that reached set_config or install_* would be an exec call made under
#: a control grant.
RUN_STEPS_TOOLS = frozenset({
    "click", "double_click", "right_click", "set_value", "type_text", "press_key",
    "hotkey", "scroll", "drag", "move_cursor",
})
#: Keys of a run_steps step that are not a tool's name.
RUN_STEPS_KEYS = frozenset({"tool", "args", "wait_for", "expect", "timeout_ms"})


def tool_scope(tool: str) -> str:
    if tool in VIEW_TOOLS:
        return vmhub.VIEW
    if tool in EXEC_TOOLS:
        return vmhub.EXEC
    return vmhub.CONTROL


def _timeout(body: dict, default: float) -> float:
    raw = body.get("timeout")
    if raw in (None, ""):
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        raise VmError("vm_bad_request", "'timeout' must be a number of seconds") from None
    if not 0 < value <= vm_tart.EXEC_MAX:
        raise VmError("vm_bad_request", f"'timeout' must be between 0 and {vm_tart.EXEC_MAX} seconds")
    return value


def check_nested(tool: str, arguments: dict) -> None:
    """Refuse a call whose inner tool names go beyond what the outer tool's
    scope covers (run_steps carries control actions only)."""
    if tool != "run_steps":
        return
    steps = arguments.get("steps")
    if not isinstance(steps, list):
        raise VmError("vm_bad_request", "run_steps: 'steps' must be a list")
    for i, step in enumerate(steps):
        if not isinstance(step, dict):
            raise VmError("vm_bad_request", f"run_steps: step {i} must be an object")
        names = [step["tool"]] if "tool" in step else []
        names += [k for k in step if k not in RUN_STEPS_KEYS]
        for name in names:
            if name not in RUN_STEPS_TOOLS:
                raise VmError("vm_tool_not_allowed",
                              f"run_steps: step {i} names {str(name)[:40]!r}, which is not one of "
                              + ", ".join(sorted(RUN_STEPS_TOOLS)))


#: View tools whose answer is a look at the screen: the only ones that satisfy
#: "look before you act" after a hand-over.
OBSERVE_TOOLS = frozenset({"get_desktop_state", "get_window_state", "zoom"})

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


def _trace(request: web.Request, reg: Registry, vm_id: str, what: str, **extra) -> None:
    """Audit a change a user route made, with the actor the request declared
    (kind, name, and how it authenticated: ``web`` cookie or ``cli`` token)."""
    actor = _caller(request)
    text = ":".join(x for x in (actor.get("kind"), actor.get("name")) if x)
    if actor.get("via"):
        text += f"/{actor['via']}"
    reg.note(vm_id, what, actor=text or "unknown", **extra)


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


#: Host details a session has no use for: where the daemon's key lives and how
#: the guest is reached. The user's page and shell still get them.
HOST_ONLY = ("ssh_key", "ssh_user", "cua_path", "channel_error")


def _for_session(row: dict, session: str) -> dict:
    """A session sees its own scopes, not the other sessions', and no host
    paths."""
    row = {k: v for k, v in row.items() if k not in HOST_ONLY}
    row["grants"] = {session: row["grants"].get(session, [])}
    return row


async def h_list(request: web.Request) -> web.Response:
    reg, _ = _hub(request)
    session = request.query.get("session", "")
    actor = _caller(request)
    if actor.get("kind") in (actors.AGENT, actors.CFLOW):
        session = _as_session(request, {"session": session or actor.get("name") or ""})
    if session:
        rows = [_for_session(reg.view(vm.id), session) for vm in _viewable(reg, session)]
    else:
        rows = [reg.view(vm.id) for vm in reg.list()]
    return web.json_response({"vms": rows, "slot_limit": reg.slot_limit})


async def h_get(request: web.Request) -> web.Response:
    reg, _ = _hub(request)
    try:
        row = reg.view(request.match_info["id"])
        actor = _caller(request)
        if actor.get("kind") in (actors.AGENT, actors.CFLOW):
            session = _as_session(request, {"session": request.query.get("session", "")
                                            or actor.get("name") or ""})
            if not row["grants"].get(session):
                raise VmError("vm_no_grant", f"{session} has no grant on {row['id']}")
            row = _for_session(row, session)
        return web.json_response(row)
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
        _trace(request, reg, vm.id, "register")
        return web.json_response(reg.view(vm.id), status=201)
    except VmError as exc:
        return _err(exc)


async def h_remove(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    try:
        reg.remove(request.match_info["id"])
        _trace(request, reg, request.match_info["id"], "remove")
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
            _trace(request, reg, vm_id, "lifecycle", action=action)
            channels: vm_channel.Hub = request.app["vm_channels"]
            if action != "start":
                # The channel goes first: it would otherwise reconnect to a
                # guest that is shutting down.
                await channels.drop(vm_id)
            # tart and ssh block: keep them off the event loop.
            try:
                await asyncio.to_thread(getattr(mgr, action), vm_id)
            except VmError:
                # The refusal left the guest up (shutdown could not start,
                # suspend failed): its channel was dropped above, bring it back.
                vm = reg.get(vm_id)
                if action != "start" and vm.state == "running" and vm.ssh_ready:
                    channels.ensure(vm_id)
                raise
            if action == "start":
                channels.ensure(vm_id)
            return web.json_response(reg.view(vm_id))
        except VmError as exc:
            return _err(exc)
    return handler


async def h_reconcile(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, mgr = _hub(request)
    try:
        _trace(request, reg, "", "reconcile")
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
        _trace(request, reg, request.match_info["id"], "user_grant", to=session, scopes=got)
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
        _trace(request, reg, request.match_info["id"], "user_revoke", to=session, left=left)
        return web.json_response({"ok": True, "grants": left})
    except VmError as exc:
        return _err(exc)


async def h_revoke_controller(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    try:
        reg.revoke_controller(request.match_info["id"])
        _trace(request, reg, request.match_info["id"], "revoke_controller")
        return web.json_response({"ok": True})
    except VmError as exc:
        return _err(exc)


async def h_extra(request: web.Request) -> web.Response:
    """Duplicate control is not offered yet: two controllers need the one
    command queue per VM (vm-setup-8s6), which is not built. The registry
    supports it; this route does not."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    try:
        raise VmError("vm_duplicate_unavailable",
                      "two sessions driving one VM waits for the per-VM command queue; "
                      "hand control over with revoke-control instead")
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
            _trace(request, reg, vm_id, "human", action=action)
            return web.json_response({"ok": True, **reg.human_driving(vm_id)})
        if action == "idle":
            _trace(request, reg, vm_id, "human", action=action)
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


def _shots_dir(vm_id: str):
    """One directory per VM, so that pruning one VM's files can never match
    another's (ids are checked names, but 'a' and 'a-b' share a prefix)."""
    d = paths.daemon_dir() / "vm" / "shots" / vmhub.check_name("id", vm_id)
    d.mkdir(parents=True, exist_ok=True)
    try:
        d.parent.chmod(0o700)
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
            path = _shots_dir(vm_id) / f"{int(_time.time() * 1000):016d}-{len(content)}.{ext}"
            path.write_bytes(base64.b64decode(block["data"]))
            content.append({"type": "image", "path": str(path), "mimeType": mime})
        else:
            content.append(block)
    old = sorted(p for p in _shots_dir(vm_id).iterdir() if p.is_file())[:-SHOTS_KEEP]
    for f in old:
        try:
            f.unlink()
        except OSError:
            pass
    return {**result, "content": content}


PAGE_FILE = Path(__file__).resolve().parent.parent / "web" / "static" / "vm.html"


async def h_frame(request: web.Request) -> web.Response:
    """The newest screen frame (JPEG). ``?since=<seq>`` waits for a newer one;
    304 when there is none. A session sees it only with a view grant."""
    reg, _ = _hub(request)
    vm_id = request.match_info["id"]
    try:
        actor = _caller(request)
        if actor.get("kind") in (actors.AGENT, actors.CFLOW):
            session = _as_session(request, {"session": request.query.get("session", "")})
            if vmhub.VIEW not in reg.get(vm_id).grants.get(session, []):
                raise VmError("vm_no_grant", f"{session} has no view grant on {vm_id}")
        try:
            since = int(request.query.get("since") or 0)
        except ValueError:
            since = 0
        frame = await request.app["vm_frames"].get(vm_id, since)
    except VmError as exc:
        return _err(exc)
    headers = {"Cache-Control": "no-store", "X-Frame-Seq": str(frame.seq)}
    if frame.seq <= since:
        return web.Response(status=304, headers=headers)
    if not frame.data:
        return web.json_response({"ok": False, "code": "vm_no_frame", "error": frame.error or
                                  "no frame yet", "seq": frame.seq}, status=503, headers=headers)
    return web.Response(body=frame.data, content_type="image/jpeg", headers=headers)


async def h_page(_request: web.Request) -> web.StreamResponse:
    if PAGE_FILE is None or not PAGE_FILE.is_file():
        return web.Response(status=404, text="the VM page assets are missing")
    return web.FileResponse(PAGE_FILE, headers={"Cache-Control": "no-cache"})


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
        check_nested(tool, args)
        timeout = _timeout(body, 30)
        scope = tool_scope(tool)
        reg.begin_call(vm_id, session, scope, body.get("epoch"))
        ok = False
        try:
            channel = request.app["vm_channels"].get(vm_id)
            result = await channel.call(tool, args, timeout)
            ok = True
        finally:
            reg.end_call(vm_id, session, scope, ok=ok, observe=tool in OBSERVE_TOOLS)
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
            {"name": t.get("name"), "scope": tool_scope(str(t.get("name"))),
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
        timeout = _timeout(body, 60)
        reg.begin_call(vm_id, session, vmhub.EXEC, body.get("epoch"))
        ok = False
        try:
            out = await asyncio.to_thread(mgr.exec, vm_id, command, timeout)
            ok = out["rc"] == 0
        finally:
            reg.end_call(vm_id, session, vmhub.EXEC, ok=ok)
        return web.json_response({"ok": True, **out})
    except VmError as exc:
        return _err(exc)


async def _background(app: web.Application):
    reg: Registry = app["vm_registry"]
    mgr: vm_tart.Manager = app["vm_manager"]

    await asyncio.to_thread(reg.load_and_reset)
    known = {v.id for v in reg.list()}  # what the file held: the recovery's whole business

    async def recover():
        """After a (re)start: make the records agree with tart, then probe each
        running guest and open its channel, one VM at a time failing alone.
        Runs behind the daemon's start, not in front of it."""
        delay = 5.0
        while True:
            try:
                await asyncio.to_thread(mgr.reconcile, known)
                break
            except VmError:
                # tart unreadable: nothing is ready (after_restart cleared it),
                # so nothing is used meanwhile; try again.
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)

        async def one(vm_id: str):
            for attempt in range(3):
                try:
                    await mgr.reprobe(vm_id)
                    app["vm_channels"].ensure(vm_id)
                    return
                except VmError as exc:
                    reg.note(vm_id, "reprobe_failed", code=exc.code, attempt=attempt + 1)
                    await asyncio.sleep(10.0)

        await asyncio.gather(*(one(v.id) for v in reg.list() if v.id in known and v.state == "running"),
                             return_exceptions=True)

    recovery = asyncio.create_task(recover()) if known else None

    async def hand_back():
        while True:
            await asyncio.sleep(2)
            idle = float(store.daemon_config().get("vm_human_idle", 15))
            for vm in reg.list():
                if vm.human == "driving":
                    reg.human_timeout(vm.id, idle)

    async def settle():
        while True:
            await asyncio.sleep(30)
            try:
                await asyncio.to_thread(mgr.settle)
            except VmError:
                pass

    task = asyncio.create_task(hand_back())
    settling = asyncio.create_task(settle())
    yield
    for t in (task, settling, recovery):
        if t is not None:
            t.cancel()
    await app["vm_frames"].stop_all()
    await app["vm_channels"].drop_all()


def install(app: web.Application, registry: Optional[Registry] = None,
            manager: Optional[vm_tart.Manager] = None,
            channels: Optional[vm_channel.Hub] = None,
            frames: Optional[vm_frames.Frames] = None) -> None:
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
    app["vm_frames"] = frames or vm_frames.Frames(reg)
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
    r.add_get("/api/vm/{id}/frame", h_frame)
    r.add_get("/vm", h_page)
