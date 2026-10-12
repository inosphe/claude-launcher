"""Local synchronization request API; provisioning is an explicit human decision.

Unknown service profile, mesh membership or authority is never inferred from
an arbitrary requesting worker. The response carries a concrete resolution
card; a person creates a dedicated session through normal onboarding and binds
it here. This uses the same trusted-dashboard boundary as reposync approvals.
"""
from __future__ import annotations

import asyncio
import json
import socket

from aiohttp import web

from .. import local_sync, workspaces
from ..cflow2.store import RunStore
from . import paths
from .manager import ManagerError


def _registry(request):
    item = workspaces.get(request.match_info["name"])
    if not item:
        raise local_sync.PolicyError("unknown workspace")
    path = item["path"] if isinstance(item, dict) else item.path
    machine = str((request.app["relay_state"]() or {}).get("name") or socket.gethostname())
    identity = machine + ":" + str(paths.daemon_dir().resolve())
    return local_sync.Registry(path, identity, boot=request.app["boot_id"])


def _person(request):
    if request.cookies.get("claunch_session") not in request.app["cookie_sessions"]:
        raise web.HTTPForbidden(text="use the authenticated dashboard for policy or provisioning decisions")


def _member(request, registry, session, mesh):
    agent = request.app["manager"].get(session)
    if agent.exited or local_sync.common_dir(agent.sdef.cwd) != registry.path.parent:
        raise local_sync.PolicyError("manager must be live in this repository")
    memberships = request.app["mesh"].meshes_for_session(session)
    membership = next((m for m in memberships
                       if m["mesh"] == mesh and m["role"] == "local-sync-manager"), None)
    if membership is None:
        raise local_sync.PolicyError("manager needs explicit role and membership in request mesh")
    run = RunStore(agent.sdef.cwd, session)
    if (not run.exists() or run.load_chart().name != "local-sync-v2"
            or run.load_state().get("status") in ("done", "aborted")):
        raise local_sync.PolicyError("manager must run local-sync-v2")
    return membership["handle"]


async def _notify(request, registry, rid):
    manager = registry.reserve()
    if manager["state"] != "ready":
        return "explicit manager binding required"
    try:
        payload = json.loads(registry.get(rid)["payload"])
        handle = _member(request, registry, manager["session"], payload["mesh"])
        delivery = await request.app["mesh"].send(
            payload["mesh"], payload["requester"], handle,
            "Local sync request " + rid + ": use repo_sync local_status; "
            "follow local-sync-v2 and report the durable receipt.", type="say")
        if delivery.get("queued") or delivery.get("undeliverable") or delivery.get("deferred"):
            return "delivery pending: " + str(delivery.get("notice") or "mesh recipient unavailable")
        return ""
    except Exception as exc:
        # Persisted work is retained even if the caller's mesh is disconnected.
        return str(exc)


async def handle(request):
    try:
        registry = await asyncio.to_thread(_registry, request)
        action = request.match_info["action"]
        body = await request.json() if request.method == "POST" else {}
        if action == "status":
            with registry.tx() as db:
                rows = [dict(r) for r in db.execute("SELECT * FROM requests")]
            identities = {name: local_sync.remote_key(registry.repo, name)
                          for name in local_sync.reposync.remotes(registry.repo)}
            return web.json_response({"policy": local_sync.policy(registry.repo),
                                      "remote_identities": identities,
                                      "manager": registry.reserve(), "requests": rows})
        if action == "configure":
            _person(request)
            result = registry.configure(body["targets"], expected_revision=body["revision"],
                                        profile=body.get("profile", ""))
        elif action == "request":
            result = registry.submit(body)
            manager = registry.reserve()
            if manager["session"]:
                try:
                    alive = not request.app["manager"].get(manager["session"]).exited
                except Exception:
                    alive = False
                if not alive:
                    registry.retire(epoch=manager["epoch"])
                    manager = registry.reserve()
            # Missing membership for a new mesh does not revoke another mesh's
            # valid manager. Keep it and ask for the specific missing connection.
            dispatch_error = await _notify(request, registry, body["id"])
            result = {"request": result, "manager": manager,
                      "resolution": {"required": manager["state"] != "ready" or bool(dispatch_error),
                                     "dispatch_error": dispatch_error,
                                     "reason": "Explicit repository manager and mesh binding required",
                                     "options": ["create dedicated manager", "bind explicit manager", "hold", "cancel"],
                                     "workflow": "local-sync-v2", "role": "local-sync-manager",
                                     "inputs": {"request_id": body["id"], "epoch": str(manager["epoch"])},
                                     "workspace": request.match_info["name"], "mesh": body["mesh"],
                                     "notice": "Provisioning does not approve publication or PR merge"}}
        elif action == "bind":
            _person(request)
            payload = json.loads(registry.get(body["request_id"])["payload"])
            _member(request, registry, body["session"], payload["mesh"])
            result = registry.bind(body["session"], epoch=body["epoch"],
                                   role="local-sync-manager", workflow="local-sync-v2")
            result["dispatch_error"] = await _notify(request, registry, body["request_id"])
        elif action == "approve-checkout":
            _person(request)
            result = registry.approve_checkout(body["request_id"], epoch=body["epoch"],
                                               old=body["old"], new=body["new"])
            result["dispatch_error"] = await _notify(request, registry, body["request_id"])
        elif action == "resolve":
            _person(request)
            if body["state"] == "replace_manager":
                registry.retire(epoch=body["epoch"])
                result = registry.reserve()
            else:
                result = registry.settle(body["request_id"], body["state"])
                result["dispatch_error"] = await _notify(request, registry, body["request_id"])
        elif action == "apply":
            payload = json.loads(registry.get(body["request_id"])["payload"])
            _member(request, registry, body["session"], payload["mesh"])
            result = await asyncio.to_thread(registry.apply, body["request_id"],
                                            session=body["session"], epoch=body["epoch"])
        else:
            raise web.HTTPNotFound()
        return web.json_response(result)
    except web.HTTPException:
        raise
    except (KeyError, TypeError, ValueError, ManagerError, local_sync.reposync.SyncError) as exc:
        return web.json_response({"error": str(exc)}, status=409)


def install(app):
    prefix = "/api/local-sync/workspaces/{name}"
    app.router.add_get(prefix + "/{action:status}", handle)
    app.router.add_post(prefix + "/{action:configure|request|bind|resolve|apply|approve-checkout}", handle)
