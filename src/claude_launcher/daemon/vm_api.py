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
               revoke_controller,extra_controller,human,files_dir,share,
               approve,deny}, POST /api/vm-share-roots (add / rm an allowed root),
               GET /api/vm-requests (pending requests, for the dashboard),
               GET|POST /api/vm/{id}/destroy (delete the tart disk; confirm = the id),
               GET /api/vm/{id}/files
Anyone:        GET /api/vm, GET /api/vm/{id}
Session:       POST /api/vm/{id}/{acquire,release}  (body ``session`` must be
               the declared actor), POST /api/vm/{id}/files/{put,get,ls,rm}
               (``files`` grant), POST /api/vm/{id}/request (a session asks
               for scopes and shared folders; the user approves)
"""

from __future__ import annotations

import asyncio
import json
import os
from pathlib import Path
from typing import List, Optional

from aiohttp import web

from .. import store
from . import actors, paths, vm_channel, vm_files, vm_frames, vm_guard, vm_pack, vm_presets, vm_share, vm_tart, vmhub
from .vmhub import Registry, VmError

_STATUS = {
    "vm_unknown": 404, "tart_missing": 404, "vm_exists": 409, "vm_held": 409,
    "vm_slots_full": 409, "vm_bad_state": 409, "vm_busy_state": 409,
    "vm_not_ready": 409, "vm_busy": 423, "vm_stale": 409, "vm_no_grant": 403,
    "vm_not_controller": 403, "vm_stop_timeout": 504, "vm_boot_timeout": 504,
    "tart_failed": 502, "vm_stop_failed": 502, "vm_unavailable": 503,
    "vm_timeout": 504, "vm_guest_error": 502, "vm_observe_first": 409,
    "vm_bad_scope": 400, "vm_bad_request": 400, "vm_duplicate_unavailable": 501,
    "vm_tool_not_allowed": 403, "vm_suspend_timeout": 504, "vm_unknown_preset": 404, "vm_port_taken": 409, "vm_allow_too_wide": 400,
    "vm_allow_holds_host": 400, "vm_preset_exists": 409, "vm_locked": 423, "vm_only_refused": 409, "vm_share_confirm_rw": 400, "vm_request_changed": 409, "vm_lock_needs_stop": 409,
    "vm_name_taken": 409, "vm_disk_low": 507, "vm_confirm": 400, "vm_presets_unreadable": 500, "vm_host_locked": 423, "vm_bad_address": 502,
    "tart_exited": 502, "vm_no_controller": 409, "vm_reason_required": 400,
    "vm_files_unsafe": 400, "vm_files_missing": 404, "vm_files_exists": 409,
    "vm_files_failed": 502, "vm_files_limit": 413, "vm_files_busy": 409,
    "vm_share_no_roots": 409, "vm_share_outside_root": 400, "vm_share_forbidden": 400,
    "vm_share_limit": 400, "vm_preset_copy": 409, "vm_destroy_granted": 409, "vm_template_confirm": 409, "vm_root_unknown": 404,
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
            if not isinstance(name, str) or name not in RUN_STEPS_TOOLS:
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
HOST_ONLY = ("ssh_key", "ssh_user", "cua_path", "channel_error", "expose", "allow", "net_pending",
             "net_applied_known", "locked_but_running", "files_dir", "requests", "share_rows", "cloned_from", "template")


def _for_session(row: dict, session: str) -> dict:
    """A session sees its own scopes, not the other sessions', and no host
    paths."""
    shares = [{k: r[k] for k in ("name", "host_path", "mode", "guest_path")}
              for r in row.get("share_rows") or [] if r["state"] != "left_out"]
    row = {k: v for k, v in row.items() if k not in HOST_ONLY}
    row["grants"] = {session: row["grants"].get(session, [])}
    row["shared_dirs"] = shares
    if shares:
        row["share_guidance"] = vm_share.guidance(shares)
    return row


def _template(request: web.Request, row: dict) -> dict:
    """The user's row says when the VM is the copy a preset was saved as (it was
    registered before registration refused such copies): starting it changes what
    later clones of the preset start from."""
    presets = request.app.get("vm_presets")
    name = presets.copy_names().get(row["tart_name"], "") if presets is not None else ""
    return {**row, "template": name}


def _with_files(request: web.Request, row: dict, session: str) -> dict:
    """A session that holds the files grant is told where its exchange folders
    are: the only host path it is given."""
    if vmhub.FILES in row["grants"].get(session, []):
        reg, _ = _hub(request)
        row["files"] = {"host_dir": str(_files(request).host_dir(row["id"])),
                        "guest_dir": reg.get(row["id"]).work_dir}
    return row


async def h_list(request: web.Request) -> web.Response:
    reg, _ = _hub(request)
    session = request.query.get("session", "")
    actor = _caller(request)
    if actor.get("kind") in (actors.AGENT, actors.CFLOW):
        session = _as_session(request, {"session": session or actor.get("name") or ""})
    if session:
        rows = [_with_files(request, _for_session(reg.view(vm.id), session), session)
                for vm in _viewable(reg, session)]
    else:
        rows = [_template(request, reg.view(vm.id)) for vm in reg.list()]
    _, mgr = _hub(request)
    out = {"vms": rows, "slot_limit": reg.slot_limit,
           "host_locked": bool(mgr and await asyncio.to_thread(mgr.host_locked))}
    if not session and mgr is not None:     # the user's page: where a share may come from
        roots, forbidden = mgr.share_rules()
        out["share_roots"] = roots
        out["share_root_rows"] = vm_share.root_rows(store.daemon_config().get("vm_share_roots"), forbidden)
        out["presets"] = _presets(request).lineage_rows() if request.app.get("vm_presets") is not None else []
        out["share_catalog"] = [vm_share.public(e) | {"scope": e["scope"], "vms": list(e["vms"])} for e in reg.catalog]
    return web.json_response(out)


async def h_requests(request: web.Request) -> web.Response:
    """Every pending session request, for the dashboard's notice and badge (user
    only). Cheaper than ``GET /api/vm``, which also asks the host about its screen
    lock, so the dashboard can poll it."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    rows = [{"vm": vm.id, **r} for vm in reg.list() for r in reg.view(vm.id).get("requests") or []]
    return web.json_response({"requests": rows})


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
            row = _with_files(request, _for_session(row, session), session)
        else:
            row = _template(request, row)
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
        tart_name = str(body.get("tart_name") or "") or vm_id
        preset = _presets(request).copy_names().get(tart_name)
        if preset:
            raise VmError("vm_preset_copy",
                          f"tart VM {tart_name!r}는 프리셋 {preset!r}의 복사본입니다. 프리셋 복사본은 VM으로 등록하지 않습니다. "
                          f"새 VM이 필요하면 '프리셋으로 새 VM 만들기'(claunch vm preset new {preset} <id> 또는 VM 페이지의 프리셋 목록)를 쓰십시오")
        vm = reg.add(vm_id, str(body.get("tart_name") or ""),
                     ssh_user=str(body.get("ssh_user") or "admin"),
                     ssh_key=str(body.get("ssh_key") or ""))
        _trace(request, reg, vm.id, "register")
        return web.json_response(reg.view(vm.id), status=201)
    except VmError as exc:
        return _err(exc)


async def h_tart_list(request: web.Request) -> web.Response:
    """The VMs tart has on this host (local source), each marked with the
    record that holds it, for the dashboard's register form. Read only, and
    the user's: a session is told the host's VM inventory by nothing here."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, mgr = _hub(request)
    try:
        rows = await asyncio.to_thread(mgr.tart_list)
    except VmError as exc:
        return _err(exc)
    held = {vm.tart_name: vm.id for vm in reg.list()}
    copies = {item["tart_name"] for item in await asyncio.to_thread(_presets(request).list)}
    out = []
    for row in rows:
        try:
            vmhub.check_name("tart_name", row.name)
            usable = row.name not in copies  # a preset's copy is not run as a VM
        except VmError:
            usable = False
        out.append({"name": row.name, "state": row.state, "running": row.running,
                    "registered": row.name in held, "id": held.get(row.name, ""),
                    "usable": usable, "preset": row.name in copies})
    return web.json_response({"tart": out})


def _presets(request: web.Request) -> vm_presets.Presets:
    return request.app["vm_presets"]


async def h_preset_list(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    rows = await asyncio.to_thread(_presets(request).list)
    return web.json_response({"presets": rows})


async def h_preset_save(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    try:
        body = await _body(request)
        vm_id = str(body.get("vm") or "").strip()
        name = str(body.get("name") or "").strip()
        if not vm_id or not name:
            raise VmError("vm_bad_request", "'vm' and 'name' are required")
        _trace(request, reg, vm_id, "preset_save", preset=name)
        item = await asyncio.to_thread(
            _presets(request).save, vm_id, name, str(body.get("description") or ""))
        return web.json_response(item.to_dict(), status=201)
    except VmError as exc:
        return _err(exc)


async def h_preset_describe(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    name = request.match_info["name"]
    try:
        body = await _body(request)
        _trace(request, reg, "", "preset_describe", preset=name)
        item = await asyncio.to_thread(_presets(request).describe, name,
                                       str(body.get("description") or ""))
        return web.json_response(item.to_dict())
    except VmError as exc:
        return _err(exc)


async def h_preset_delete(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    name = request.match_info["name"]
    try:
        body = await _body(request)
        _trace(request, reg, "", "preset_delete", preset=name)
        await asyncio.to_thread(_presets(request).delete, name, str(body.get("confirm") or ""))
        return web.json_response({"ok": True})
    except VmError as exc:
        return _err(exc)


async def h_preset_create(request: web.Request) -> web.Response:
    """A new VM from a preset: clone, register, start. The VM is registered
    once the clone is made; a start that cannot happen (no free slot, the guest
    does not come up) leaves it registered and says why."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, mgr = _hub(request)
    name = request.match_info["name"]
    try:
        body = await _body(request)
        vm_id = str(body.get("id") or "").strip()
        if not vm_id:
            raise VmError("vm_bad_request", "'id' is required")
        _trace(request, reg, vm_id, "preset_create", preset=name)
        await asyncio.to_thread(_presets(request).create_vm, name, vm_id)
    except VmError as exc:
        return _err(exc)
    started, why = False, ""
    held = reg.holders(except_id=vm_id)
    if len(held) >= reg.slot_limit:
        why = (f"{len(held)} of {reg.slot_limit} macOS VM slots are in use ({', '.join(held)}); "
               "the new VM is registered and stopped")
    else:
        try:
            await asyncio.to_thread(mgr.start, vm_id)
            request.app["vm_channels"].ensure(vm_id)
            started = True
        except VmError as exc:
            why = f"{exc.code}: {exc}"
    return web.json_response({"vm": reg.view(vm_id), "started": started, "why": why}, status=201)


async def h_expose(request: web.Request) -> web.Response:
    """Publish or withdraw a guest TCP port on the host's egress interface
    (``tart run --net-softnet-expose``): the user's, applied at the next start."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    vm_id = request.match_info["id"]
    try:
        body = await _body(request)
        action = str(body.get("action") or "")
        if action == "add":
            _trace(request, reg, vm_id, "expose_add", external=body.get("external"),
                   internal=body.get("internal"))
            reg.expose_add(vm_id, body.get("external"), body.get("internal"))
        elif action == "rm":
            _trace(request, reg, vm_id, "expose_rm", external=body.get("external"))
            reg.expose_remove(vm_id, body.get("external"))
        else:
            raise VmError("vm_bad_request", "'action' is add or rm")
        return web.json_response(reg.view(vm_id))
    except VmError as exc:
        return _err(exc)


async def h_allow(request: web.Request) -> web.Response:
    """Let softnet pass traffic to and from an IPv4 range inside the blocked
    private ranges (``--net-softnet-allow``): the user's, narrow ranges only,
    applied at the next start."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, mgr = _hub(request)
    vm_id = request.match_info["id"]
    try:
        body = await _body(request)
        action = str(body.get("action") or "")
        if action == "add":
            addrs = await asyncio.to_thread(mgr._host_addrs)
            _trace(request, reg, vm_id, "allow_add", cidr=str(body.get("cidr")))
            reg.allow_add(vm_id, body.get("cidr"), addrs)
        elif action == "rm":
            _trace(request, reg, vm_id, "allow_rm", cidr=str(body.get("cidr")))
            reg.allow_remove(vm_id, body.get("cidr"))
        else:
            raise VmError("vm_bad_request", "'action' is add or rm")
        return web.json_response(reg.view(vm_id))
    except VmError as exc:
        return _err(exc)


async def h_lock(request: web.Request) -> web.Response:
    """Lock or unlock a VM: the user's. A locked VM is not started or called
    by anything the daemon runs until it is unlocked (docs/vm-hub.md)."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    vm_id = request.match_info["id"]
    locked = request.match_info["op"] == "lock"
    try:
        reg.get(vm_id)
        _trace(request, reg, vm_id, "lock" if locked else "unlock")
        reg.set_locked(vm_id, locked)
        return web.json_response(reg.view(vm_id))
    except VmError as exc:
        return _err(exc)


async def h_share(request: web.Request) -> web.Response:
    """Add or remove a shared host folder (``tart run --dir``): the user's, applied
    at the next start. The path is resolved and checked against the allowed
    roots and the never-shared list (``vm_share``)."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, mgr = _hub(request)
    vm_id = request.match_info["id"]
    try:
        body = await _body(request)
        action = str(body.get("action") or "")
        if action == "add":
            roots, forbidden = await asyncio.to_thread(mgr.share_rules)
            share = await asyncio.to_thread(vm_share.check_share, body.get("name"), body.get("host_path"),
                                            body.get("mode"), roots, forbidden)
            _trace(request, reg, vm_id, "share_add", name=share["name"], host_path=share["host_path"],
                   mode=share["mode"])
            reg.share_add(vm_id, share)
        elif action == "rm":
            _trace(request, reg, vm_id, "share_rm", name=str(body.get("name"))[:64])
            reg.share_remove(vm_id, str(body.get("name") or ""))
        else:
            raise VmError("vm_bad_request", "'action' is add or rm")
        return web.json_response(reg.view(vm_id))
    except VmError as exc:
        return _err(exc)


async def h_share_roots(request: web.Request) -> web.Response:
    """The folders shares may come from (``daemon.vm_share_roots``), user only: GET
    lists them with whether each is in effect; POST ``add`` / ``rm`` by path. A
    root that is the home folder, holds or lies under a never-shared folder, or is
    not a folder is refused when added. Only that one setting is written; the
    manager reads it afresh at every share and start, so no restart is needed."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, mgr = _hub(request)
    try:
        forbidden = await asyncio.to_thread(mgr.share_rules)
        forbidden = forbidden[1]
        raw = store.daemon_config().get("vm_share_roots")
        stored = [r for r in raw if isinstance(r, str)] if isinstance(raw, list) else []
        if request.method == "POST":
            body = await _body(request)
            action, path = str(body.get("action") or ""), body.get("path")
            if action == "add":
                real = await asyncio.to_thread(vm_share.check_root, path, forbidden)
                if real not in stored and len(stored) >= vm_share.ROOTS_MAX:
                    raise VmError("vm_share_limit", f"공유 루트는 최대 {vm_share.ROOTS_MAX}개입니다")
                _trace(request, reg, "", "share_root_add", path=real)
                if real not in stored:
                    stored.append(real)
            elif action == "rm":
                if not isinstance(path, str) or not path:
                    raise VmError("vm_bad_request", "'path' is required")
                keep = [r for r in stored if r != path and os.path.realpath(r) != os.path.realpath(path)]
                if len(keep) == len(stored):
                    raise VmError("vm_root_unknown", f"허용 루트에 {path[:120]!r}가 없습니다")
                _trace(request, reg, "", "share_root_rm", path=path[:300])
                stored = keep
            else:
                raise VmError("vm_bad_request", "'action' is add or rm")
            store.set_daemon_field("vm_share_roots", stored)
        rows = vm_share.root_rows(stored, forbidden)
        return web.json_response({"roots": rows, "enabled": any(r["ok"] for r in rows)})
    except VmError as exc:
        return _err(exc)


async def h_share_catalog(request: web.Request) -> web.Response:
    """The share catalog (user only): GET lists it with the allowed roots; POST
    ``put`` (add; ``replace`` changes an entry) or ``rm`` by name. An entry
    for all VMs that is read-write needs ``confirm_rw: true`` -- the page asks, the
    CLI has ``--yes-rw``. Applied to a VM at its next start."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, mgr = _hub(request)
    try:
        if request.method == "POST":
            body = await _body(request)
            action = str(body.get("action") or "")
            if action == "put":
                roots, forbidden = await asyncio.to_thread(mgr.share_rules)
                entry = await asyncio.to_thread(vm_share.check_catalog_entry, body, roots, forbidden,
                                                [v.id for v in reg.list()])
                if entry["scope"] == vm_share.SCOPE_ALL and entry["mode"] == "rw" and body.get("confirm_rw") is not True:
                    raise VmError("vm_share_confirm_rw",
                                  "모든 VM에 쓰기 가능(rw)으로 공유하려면 확인이 필요합니다(confirm_rw / --yes-rw)")
                _trace(request, reg, "", "catalog_put", name=entry["name"], host_path=entry["host_path"],
                       mode=entry["mode"], scope=entry["scope"], vms=entry["vms"])
                reg.catalog_put(entry, replace=body.get("replace") is True)
            elif action == "rm":
                _trace(request, reg, "", "catalog_rm", name=str(body.get("name"))[:64])
                reg.catalog_remove(str(body.get("name") or ""))
            else:
                raise VmError("vm_bad_request", "'action' is put or rm")
        roots = (await asyncio.to_thread(mgr.share_rules))[0]
        return web.json_response({"share_catalog": [vm_share.public(e) | {"scope": e["scope"], "vms": list(e["vms"])}
                                                    for e in reg.catalog], "share_roots": roots})
    except VmError as exc:
        return _err(exc)


def _only_rows(request: web.Request, reg: Registry) -> List[dict]:
    """One row per session that holds a VM grant or has VM-only mode on: the switch,
    why it cannot be turned on, and the recent blocks."""
    manager = request.app.get("manager")
    names = sorted({s for vm in reg.list() for s, sc in vm.grants.items() if sc})
    rows = []
    for name in names:
        harness = ""
        try:
            harness = manager.get(name).sdef.harness if manager is not None else ""
        except Exception:  # noqa: BLE001 - an exited or unknown session
            harness = ""
        row = vm_guard.status(name, harness)
        row["limits"] = vm_guard.LIMITS
        rows.append(row)
    return rows


async def h_only(request: web.Request) -> web.Response:
    """VM-only mode (user only): GET the sessions with a VM grant and their switch;
    POST ``{session, on}`` turns it on or off. Turning it on is refused with the
    reason when the session's process has no guard hook. Takes effect on the
    session's next Bash call."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    try:
        if request.method == "POST":
            body = await _body(request)
            name = str(body.get("session") or "")
            if not name or not isinstance(body.get("on"), bool):
                raise VmError("vm_bad_request", "'session'과 불리언 'on'이 필요합니다")
            manager = request.app.get("manager")
            if body["on"]:
                try:
                    harness = manager.get(name).sdef.harness
                except Exception:  # noqa: BLE001
                    raise VmError("vm_bad_request", f"세션 {name[:64]}이(가) 없습니다") from None
                ok, why = vm_guard.can_enable(name, harness)
                if not ok:
                    raise VmError("vm_only_refused", why)
            _trace(request, reg, "", "vm_only_on" if body["on"] else "vm_only_off", target=name)
            await asyncio.to_thread(vm_guard.set_on, name, body["on"])
        return web.json_response({"sessions": await asyncio.to_thread(_only_rows, request, reg)})
    except VmError as exc:
        return _err(exc)


async def h_request(request: web.Request) -> web.Response:
    """A session asks for scopes and shared folders in one go. It grants nothing:
    the request waits for the user (``approve`` / ``deny``). The folders are
    checked now, against the same rules as at approval, so the user is never asked
    about one that could not be shared."""
    reg, mgr = _hub(request)
    vm_id = request.match_info["id"]
    try:
        body = await _body(request)
        session = _as_session(request, body)
        scopes, shares_in = body.get("scopes") or [], body.get("shares") or []
        if (not isinstance(scopes, list) or not isinstance(shares_in, list)
                or not all(isinstance(s, dict) for s in shares_in) or len(shares_in) > vm_share.SHARE_MAX):
            raise VmError("vm_bad_request", "'scopes' is a list of scope names and 'shares' a list of {name, host_path, mode}")
        if not scopes and not shares_in:
            raise VmError("vm_bad_request", "scopes나 shares 중 하나는 필요합니다")
        roots, forbidden = await asyncio.to_thread(mgr.share_rules)
        shares = [await asyncio.to_thread(vm_share.check_share, s.get("name"), s.get("host_path"),
                                          s.get("mode"), roots, forbidden) for s in shares_in]
        row = reg.request_add(vm_id, session, scopes, shares, str(body.get("reason") or ""))
        return web.json_response({
            "ok": True, "pending": True,
            "requested": {"scopes": row["scopes"], "shares": [vm_share.public(s) for s in row["shares"]]},
            "message": "사용자의 승인을 기다립니다. 승인되면 vm_list에 권한과 공유 폴더가 나타납니다. "
                       "공유 폴더는 VM을 다음에 시작할 때 마운트됩니다"})
    except VmError as exc:
        return _err(exc)


def _shown_request(reg, vm_id: str, session: str, body: dict) -> dict:
    """The pending request the user's answer is for. The answer names the request
    by the id it was shown with: a session that asked again in the meantime
    (the new request replaces the old one) is not approved unseen."""
    row = reg.request_get(vm_id, session)
    if row is None:
        raise VmError("vm_bad_request", f"{session}의 대기 중인 요청이 없습니다")
    if not isinstance(body.get("id"), str) or body["id"] != row["id"]:
        raise VmError("vm_request_changed",
                      f"{session}의 요청이 화면에 표시된 뒤 바뀌었거나 id가 없습니다. 목록을 새로 고쳐 현재 요청을 확인하십시오")
    return row


async def h_approve(request: web.Request) -> web.Response:
    """The user approves a session's request: its scopes are granted and its
    shared folders added in one step (re-checked now; nothing is applied when
    any check fails)."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, mgr = _hub(request)
    vm_id = request.match_info["id"]
    try:
        body = await _body(request)
        session = str(body.get("session") or "")
        row = _shown_request(reg, vm_id, session, body)
        roots, forbidden = await asyncio.to_thread(mgr.share_rules)
        checked = [await asyncio.to_thread(vm_share.check_share, s["name"], s["host_path"], s["mode"],
                                           roots, forbidden) for s in row["shares"]]
        vm = reg.get(vm_id)
        names = {s["name"] for s in vm.shared_dirs}
        if any(s["name"] in names for s in checked) or len(vm.shared_dirs) + len(checked) > vm_share.SHARE_MAX:
            raise VmError("vm_bad_request", "공유 이름이 겹치거나 공유 폴더가 최대 개수를 넘습니다. 요청을 거절하고 다시 받으십시오")
        _trace(request, reg, vm_id, "user_approve", to=session, scopes=row["scopes"],
               shares=[f"{s['name']}:{s['mode']}" for s in checked])
        if row["scopes"]:
            reg.grant(vm_id, session, row["scopes"])
        for share in checked:
            reg.share_add(vm_id, {**share, "origin": vm_share.make_origin(vm_share.ORIGIN_REQUEST, session)})
        reg.request_drop(vm_id, session)
        await vm_pack.sync(request.app.get("manager"), reg, session)
        return web.json_response(reg.view(vm_id))
    except VmError as exc:
        return _err(exc)


async def h_deny(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    vm_id = request.match_info["id"]
    try:
        body = await _body(request)
        session = str(body.get("session") or "")
        _shown_request(reg, vm_id, session, body)
        _trace(request, reg, vm_id, "user_deny", to=session)
        reg.request_drop(vm_id, session)
        return web.json_response(reg.view(vm_id))
    except VmError as exc:
        return _err(exc)


def _destroy_plan(request: web.Request, vm_id: str, tart_row) -> dict:
    """What deleting the VM's tart disk would do and what stands in the way. ``tart_row``
    is tart's own row for it (``None``: tart does not list it). ``blockers`` refuse the
    delete; ``warnings`` are shown and need the typed confirmation, nothing more."""
    reg, _ = _hub(request)
    vm = reg.get(vm_id)
    presets = request.app.get("vm_presets")
    lineage = presets.lineage_rows() if presets is not None else []
    copies = presets.copy_names() if presets is not None else {}
    mine = [p for p in lineage if p["source"] == vm_id]
    kids = [v.id for v in reg.list() if v.id != vm_id and v.cloned_from.get("kind") == "preset"
            and any(v.cloned_from.get("preset") == p["name"] for p in mine)]
    template = copies.get(vm.tart_name, "")
    blockers: List[dict] = []

    def block(code: str, reason: str) -> None:
        blockers.append({"code": code, "reason": reason})

    if vm_id in request.app.get("vm_destroying", ()):
        block("vm_busy_state", "이 VM의 삭제가 이미 진행 중입니다")
    if vm.state != "stopped":
        block("vm_bad_state", f"{vm_id}는 {vm.state} 상태입니다. 정상 종료해 stopped가 된 VM만 삭제할 수 있습니다"
                              "(일시 중단·실행 중·오류 상태는 삭제하지 않습니다)")
    if vm.locked:
        block("vm_locked", f"{vm_id}는 잠겨 있습니다. 먼저 잠금을 해제하십시오")
    if vm.grants or vm.controller:
        who = ", ".join(sorted(set(vm.grants) | ({vm.controller} if vm.controller else set())))
        block("vm_destroy_granted", f"세션이 이 VM의 권한을 가지고 있습니다({who}). 먼저 부여를 회수하십시오")
    if any(o.id != vm_id and o.tart_name == vm.tart_name for o in reg.list()):
        block("vm_held", f"같은 tart VM {vm.tart_name!r}를 다른 등록이 쓰고 있습니다")
    if tart_row is not None and tart_row.state != "stopped":
        block("vm_bad_state", f"tart는 {vm.tart_name}를 {tart_row.state}로 보고합니다")
    warnings: List[str] = []
    if tart_row is None:
        warnings.append(f"tart에 {vm.tart_name!r}가 없습니다. 등록 기록만 지워집니다")
    if template:
        warnings.append(f"이 VM은 프리셋 템플릿입니다: 프리셋 {template!r}의 복사본입니다. 삭제하면 이 프리셋으로 더 이상 새 VM을 "
                        "만들 수 없습니다(프리셋 삭제는 프리셋 목록에서 합니다)")
    if mine:
        warnings.append("이 VM에서 만든 프리셋: " + ", ".join(p["name"] for p in mine)
                        + ". 프리셋의 복사본(preset-<이름>)은 남지만 원본 VM이 사라집니다")
    if kids:
        warnings.append("이 VM에서 만든 프리셋으로 만든 VM(후손): " + ", ".join(kids)
                        + ". 후손의 디스크는 독립이라 영향이 없습니다")
    return {"id": vm_id, "tart_name": vm.tart_name, "state": vm.state, "locked": vm.locked,
            "size_gb": tart_row.size_gb if tart_row is not None else 0,
            "disk_gb": tart_row.disk_gb if tart_row is not None else 0,
            "tart_present": tart_row is not None, "template": template,
            "presets": [p["name"] for p in mine], "descendants": kids,
            "blockers": blockers, "warnings": warnings}


async def h_destroy(request: web.Request) -> web.Response:
    """Delete a VM's tart disk for good (user only). GET shows what that would do and
    what stands in the way; POST needs ``confirm`` equal to the VM's id. Only a stopped,
    unlocked VM nobody holds a grant on; tart's failure leaves the registration as it is."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, mgr = _hub(request)
    vm_id = request.match_info["id"]
    try:
        vm = reg.get(vm_id)
        body = await _body(request) if request.method == "POST" else {}
        if request.method == "POST" and body.get("confirm") != vm_id:
            raise VmError("vm_confirm", f"say which VM to delete: confirm must be {vm_id!r}")
        tart_row = await asyncio.to_thread(mgr.tart_find, vm.tart_name)
        plan = _destroy_plan(request, vm_id, tart_row)
        if request.method == "GET":
            return web.json_response(plan)
        if plan["blockers"]:
            first = plan["blockers"][0]
            raise VmError(first["code"], first["reason"])
        flight: set = request.app.setdefault("vm_destroying", set())
        flight.add(vm_id)
        try:
            _trace(request, reg, vm_id, "destroy", tart_name=vm.tart_name, size_gb=plan["size_gb"],
                   presets=plan["presets"], descendants=plan["descendants"], template=plan["template"])
            try:
                if plan["tart_present"]:
                    await asyncio.to_thread(mgr.tart_delete, vm.tart_name)
            except VmError as exc:
                reg.note(vm_id, "destroy_failed", error=str(exc)[:300])
                raise
            reg.remove(vm_id)               # the record, grants, requests and catalog entries go with it
            reg.note(vm_id, "destroyed", tart_name=vm.tart_name, size_gb=plan["size_gb"])
        finally:
            flight.discard(vm_id)
        return web.json_response({"ok": True, "destroyed": vm_id, "tart_name": vm.tart_name,
                                  "size_gb": plan["size_gb"], "warnings": plan["warnings"]})
    except VmError as exc:
        return _err(exc)


async def h_remove(request: web.Request) -> web.Response:
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    try:
        held_by = list(reg.get(request.match_info["id"]).grants)
        tart_name = reg.get(request.match_info["id"]).tart_name
        reg.remove(request.match_info["id"])
        _trace(request, reg, request.match_info["id"], "unregister", tart_name=tart_name)
        for sess in held_by:
            await vm_pack.sync(request.app.get("manager"), reg, sess)
        return web.json_response({"ok": True, "unregistered": request.match_info["id"], "tart_kept": tart_name})
    except VmError as exc:
        return _err(exc)


def _lifecycle_route(action: str):
    async def handler(request: web.Request) -> web.Response:
        if (refused := _user_only(request)) is not None:
            return refused
        reg, mgr = _hub(request)
        vm_id = request.match_info["id"]
        try:
            vm = reg.get(vm_id)
            if action == "start" and vm_id in request.app.get("vm_destroying", ()):
                raise VmError("vm_busy_state", f"{vm_id}의 삭제가 진행 중입니다")
            if action == "start":
                preset = _template(request, {"tart_name": vm.tart_name})["template"]
                try:
                    confirmed = (await request.json()).get("confirm_template") is True
                except (json.JSONDecodeError, UnicodeDecodeError, AttributeError):
                    confirmed = False
                if preset and not confirmed:
                    raise VmError("vm_template_confirm",
                                  f"{vm_id}는 프리셋 {preset!r}의 복사본입니다. 시작해서 쓰면 이후 이 프리셋으로 만드는 "
                                  "복제본이 달라집니다. 정말 시작하려면 confirm_template(CLI: --yes-template)을 보내십시오")
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
        changed = await asyncio.to_thread(mgr.reconcile)
    except VmError as exc:
        return _err(exc)
    # A guest adopted as running has no readiness yet: probe it and open its
    # channel, as the restart recovery does.
    probing = [v.id for v in reg.list() if v.state == "running" and not v.ssh_ready]
    for vm_id in probing:
        _spawn(request.app, _bring_up(request.app, vm_id))
    return web.json_response({"changed": changed, "probing": probing})


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
        await vm_pack.sync(request.app.get("manager"), reg, session)
        out = {"ok": True, "grants": got}
        preset = _template(request, {"tart_name": reg.get(request.match_info["id"]).tart_name})["template"]
        if preset:
            out["warning"] = (f"{request.match_info['id']}는 프리셋 {preset!r}의 복사본입니다. "
                              "세션이 이 VM을 바꾸면 이후 그 프리셋으로 만드는 복제본이 바뀝니다")
        return web.json_response(out)
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
        await vm_pack.sync(request.app.get("manager"), reg, session)
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
        shares = [{k: r[k] for k in ("name", "host_path", "mode", "guest_path")}
                  for r in reg.session_shares(request.match_info["id"])]
        if result.get("ok") and shares:
            result["shared_dirs"] = shares
            result["share_guidance"] = vm_share.guidance(shares)
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
            if reg.get(vm_id).locked:
                raise vmhub.locked_error(reg.get(vm_id))
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
        began = reg.begin_call(vm_id, session, scope, body.get("epoch")).observe_gen
        ok = False
        try:
            channel = request.app["vm_channels"].get(vm_id)
            result = await channel.call(tool, args, timeout)
            ok = True
        finally:
            reg.end_call(vm_id, session, scope, ok=ok, observe=tool in OBSERVE_TOOLS,
                         began_at_gen=began)
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


def _files(request: web.Request) -> vm_files.Files:
    return request.app["vm_files"]


async def h_files_info(request: web.Request) -> web.Response:
    """The user's view of a VM's file exchange: the two folders, the limits and
    the recent transfers."""
    if (refused := _user_only(request)) is not None:
        return refused
    try:
        return web.json_response(_files(request).describe(request.match_info["id"]))
    except VmError as exc:
        return _err(exc)


async def h_files_dir(request: web.Request) -> web.Response:
    """Set the guest work folder ("" for the default): the user's only."""
    if (refused := _user_only(request)) is not None:
        return refused
    reg, _ = _hub(request)
    vm_id = request.match_info["id"]
    try:
        body = await _body(request)
        value = body.get("dir")
        if not isinstance(value, str):
            raise VmError("vm_bad_request", "'dir' is required (\"\" for the default)")
        _trace(request, reg, vm_id, "files_dir", dir=value[:200])
        reg.set_files_dir(vm_id, value.strip())
        return web.json_response(_files(request).describe(vm_id))
    except VmError as exc:
        return _err(exc)


async def h_files(request: web.Request) -> web.Response:
    """put / get / ls / rm: a session with a ``files`` grant. The grant, the
    lock and the guest agent are checked by ``begin_call``; the paths by
    ``vm_files``."""
    reg, _ = _hub(request)
    vm_id, op = request.match_info["id"], request.match_info["op"]
    try:
        body = await _body(request)
        session = _as_session(request, body)
        reg.begin_call(vm_id, session, vmhub.FILES)
        files = _files(request)
        calls = {
            "put": lambda: files.put(vm_id, session, body.get("path"), body.get("dest"),
                                     body.get("overwrite") is True),
            "get": lambda: files.get(vm_id, session, body.get("path"), body.get("dest"),
                                     body.get("overwrite") is True),
            "ls": lambda: files.ls(vm_id, session, body.get("path")),
            "rm": lambda: files.rm(vm_id, session, body.get("path"), body.get("confirm")),
        }
        ok = False
        try:
            out = await asyncio.to_thread(calls[op])
            ok = True
        finally:
            reg.end_call(vm_id, session, vmhub.FILES, ok=ok)
        if op in ("put", "get", "ls"):
            out["host_dir"] = str(files.host_dir(vm_id))
        return web.json_response(out)
    except VmError as exc:
        return _err(exc)


def _spawn(app: web.Application, coro) -> None:
    """Run ``coro`` in the background with a reference held until it ends."""
    tasks: set = app.setdefault("vm_tasks", set())
    task = asyncio.create_task(coro)
    tasks.add(task)
    task.add_done_callback(tasks.discard)


async def _bring_up(app: web.Application, vm_id: str, tries: int = 3) -> bool:
    """Probe a running guest's ssh and console, then open its channel. After
    ``tries`` failures the VM is put in ``fault`` with the reason, so it shows
    and a later reconcile (or a stop) deals with it."""
    reg: Registry = app["vm_registry"]
    mgr: vm_tart.Manager = app["vm_manager"]
    for attempt in range(tries):
        try:
            await mgr.reprobe(vm_id)
            if not reg.get(vm_id).locked:   # a locked VM's guest is not driven
                app["vm_channels"].ensure(vm_id)
            return True
        except VmError as exc:
            reg.note(vm_id, "reprobe_failed", code=exc.code, attempt=attempt + 1)
            last = f"{exc.code}: {exc}"
            if attempt + 1 < tries:
                await asyncio.sleep(10.0)
    try:
        if reg.get(vm_id).state == "running":
            reg.set_state(vm_id, "fault", reason=f"the guest did not come up after {tries} probes ({last})")
    except VmError:
        pass
    return False


RECONCILE_TRIES = 8


async def _background(app: web.Application):
    reg: Registry = app["vm_registry"]
    mgr: vm_tart.Manager = app["vm_manager"]

    await asyncio.to_thread(reg.load_and_reset)
    known = {v.id for v in reg.list()}  # what the file held: the recovery's whole business

    async def recover():
        """After a (re)start: make the records agree with tart, then bring each
        running guest up, one VM at a time failing alone. Runs behind the
        daemon's start, not in front of it."""
        delay = 5.0
        for attempt in range(RECONCILE_TRIES):
            try:
                await asyncio.to_thread(mgr.reconcile, known)
                break
            except VmError as exc:
                # tart unreadable: nothing is ready (after_restart cleared it),
                # so nothing is used meanwhile; try again.
                reason = f"{exc.code}: {exc}"
                await asyncio.sleep(delay)
                delay = min(delay * 2, 60.0)
        else:
            for vm in reg.list():
                if vm.id in known and vm.state not in ("stopped", "suspended", "fault"):
                    try:
                        reg.set_state(vm.id, "fault", reason=f"tart could not be read after a restart ({reason})")
                    except VmError:
                        pass
            return
        await asyncio.gather(*(_bring_up(app, v.id) for v in reg.list()
                               if v.id in known and v.state == "running"),
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
                moved = await asyncio.to_thread(mgr.settle)
            except VmError:
                continue
            for vm_id in moved:  # a suspend given up on: the guest is up again
                vm = reg.get(vm_id)
                if vm.state == "running" and vm.ssh_ready:
                    app["vm_channels"].ensure(vm_id)

    task = asyncio.create_task(hand_back())
    settling = asyncio.create_task(settle())
    yield
    for t in (task, settling, recovery, *app.get("vm_tasks", ())):
        if t is not None:
            t.cancel()
    await app["vm_frames"].stop_all()
    await app["vm_channels"].drop_all()


def exit_hook(app: web.Application):
    """The manager's exit hook: a session that ends gives back the VM control it
    held (its grants stay, so a resumed session of the same name keeps its
    access). A session's name is ``session.sdef.name``."""
    def hook(session) -> None:
        name = getattr(getattr(session, "sdef", None), "name", "")
        if name:
            app["vm_registry"].session_exited(name)
    return hook


def install(app: web.Application, registry: Optional[Registry] = None,
            manager: Optional[vm_tart.Manager] = None,
            channels: Optional[vm_channel.Hub] = None,
            frames: Optional[vm_frames.Frames] = None,
            presets: Optional[vm_presets.Presets] = None,
            files: Optional[vm_files.Files] = None) -> None:
    cfg = store.daemon_config()
    reg = registry or Registry(paths.vm_json(), slot_limit=int(cfg.get("vm_slot_limit", 2)))
    app["vm_registry"] = reg
    app["vm_manager"] = manager or vm_tart.Manager(
        reg, log_dir=paths.daemon_dir() / "vm",
        share_roots=lambda: store.daemon_config().get("vm_share_roots") or [],
        share_forbidden=lambda: vm_share.forbidden_paths(state=paths.daemon_dir()),
        graphics=bool(cfg.get("vm_graphics", True)),
        stop_wait=float(cfg.get("vm_stop_wait", 120)),
        **dict(zip(("softnet", "softnet_block"),
                   vm_tart.parse_softnet(cfg.get("vm_softnet", True),
                                         cfg.get("vm_softnet_block", vm_tart.SOFTNET_BLOCK)))),
    )
    app["vm_presets"] = presets or vm_presets.Presets(
        paths.vm_presets_json(), reg, app["vm_manager"])
    app["vm_files"] = files or vm_files.Files(
        reg, app["vm_manager"], paths.daemon_dir() / "vm",
        **vm_files.limits_from(cfg))
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
    r.add_get("/api/vm-tart", h_tart_list)
    r.add_get("/api/vm-presets", h_preset_list)
    r.add_post("/api/vm-presets", h_preset_save)
    r.add_post("/api/vm-presets/{name}/describe", h_preset_describe)
    r.add_post("/api/vm-presets/{name}/delete", h_preset_delete)
    r.add_post("/api/vm-presets/{name}/create", h_preset_create)
    r.add_post("/api/vm/{id}/expose", h_expose)
    r.add_post("/api/vm/{id}/allow", h_allow)
    r.add_get("/api/vm-only", h_only)
    r.add_post("/api/vm-only", h_only)
    r.add_get("/api/vm/{id}/destroy", h_destroy)
    r.add_post("/api/vm/{id}/destroy", h_destroy)
    r.add_get("/api/vm-requests", h_requests)
    r.add_get("/api/vm-share-roots", h_share_roots)
    r.add_post("/api/vm-share-roots", h_share_roots)
    r.add_get("/api/vm-share-catalog", h_share_catalog)
    r.add_post("/api/vm-share-catalog", h_share_catalog)
    r.add_post("/api/vm/{id}/share", h_share)
    r.add_post("/api/vm/{id}/request", h_request)
    r.add_post("/api/vm/{id}/approve", h_approve)
    r.add_post("/api/vm/{id}/deny", h_deny)
    r.add_post("/api/vm/{id}/{op:lock|unlock}", h_lock)
    r.add_post("/api/vm/{id}/grant", h_grant)
    r.add_post("/api/vm/{id}/revoke", h_revoke)
    r.add_post("/api/vm/{id}/revoke_controller", h_revoke_controller)
    r.add_post("/api/vm/{id}/extra_controller", h_extra)
    r.add_post("/api/vm/{id}/human", h_human)
    r.add_post("/api/vm/{id}/acquire", h_acquire)
    r.add_post("/api/vm/{id}/release", h_release)
    r.add_post("/api/vm/{id}/call", h_call)
    r.add_post("/api/vm/{id}/exec", h_exec)
    r.add_get("/api/vm/{id}/files", h_files_info)
    r.add_post("/api/vm/{id}/files_dir", h_files_dir)
    r.add_post("/api/vm/{id}/files/{op:put|get|ls|rm}", h_files)
    r.add_get("/api/vm/{id}/tools", h_tools)
    r.add_get("/api/vm/{id}/frame", h_frame)
    r.add_get("/vm", h_page)
