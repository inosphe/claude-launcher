"""The sync server's HTTP surface.

Every ``/api/sync/*`` route except ``/api/sync/health`` needs a Bearer token
belonging to an account (see :mod:`users`) that owns the namespace in the path.
The token never appears in a URL, so it cannot leak into access logs.

Routes::

    GET  /api/sync/health              -> {"ok": true, ...}                (open)
    GET  /api/sync/whoami              -> {"user": ..., "namespaces": [...]}
    GET  /api/sync/namespaces          -> {"namespaces": [...]}
    GET  /api/sync/doc/{namespace}     -> {"revision": N, "doc": {...}, ...}
    PUT  /api/sync/doc/{namespace}     -> same, after storing (409 on a stale
                                          revision, with the winning document)
    DELETE /api/sync/doc/{namespace}   -> {"deleted": bool}

A write body is ``{"revision": <the revision you read>, "doc": {...}}``. Passing
revision 0 means "I believe this namespace has no document yet".
"""

from __future__ import annotations

import asyncio
import json
from collections import defaultdict
from typing import Optional

from aiohttp import web

from .. import __version__
from .docs import DocStore, RevisionMismatch, SyncServerError, validate_namespace
from .users import UserStore

#: Reject absurd documents before parsing: a profile config is kilobytes.
MAX_BODY_BYTES = 4 * 1024 * 1024


def json_error(status: int, message: str, **extra) -> web.Response:
    return web.json_response({"error": message, **extra}, status=status)


@web.middleware
async def error_middleware(request: web.Request, handler):
    try:
        return await handler(request)
    except RevisionMismatch as exc:
        # The client needs the winner to merge against, so hand it back with the
        # rejection rather than making it issue a second round trip.
        namespace = request.match_info.get("namespace")
        if namespace is None:
            return json_error(409, str(exc))
        store: DocStore = request.app["docs"]
        return json_error(409, str(exc), **store.read(namespace).payload())
    except SyncServerError as exc:
        return json_error(400, str(exc))
    except web.HTTPException:
        raise


def build_auth_middleware(users: UserStore):
    @web.middleware
    async def auth_middleware(request: web.Request, handler):
        path = request.path
        if not path.startswith("/api/sync/") or path == "/api/sync/health":
            return await handler(request)
        auth = request.headers.get("Authorization", "")
        if not auth.startswith("Bearer "):
            return json_error(401, "authentication required")
        user = users.authenticate(auth[7:].strip())
        if user is None:
            return json_error(401, "unknown token")
        request["user"] = user
        namespace = request.match_info.get("namespace")
        if namespace is not None:
            try:
                namespace = validate_namespace(namespace)
            except SyncServerError as exc:
                return json_error(400, str(exc))
            if not user.may_access(namespace):
                # Deliberately the same answer whether or not the namespace
                # exists — membership of someone else's namespace is not the
                # caller's business.
                return json_error(403, f"no access to namespace {namespace!r}")
        return await handler(request)

    return auth_middleware


async def _read_json(request: web.Request) -> dict:
    if request.content_length and request.content_length > MAX_BODY_BYTES:
        raise SyncServerError("document too large")
    raw = await request.content.read(MAX_BODY_BYTES + 1)
    if len(raw) > MAX_BODY_BYTES:
        raise SyncServerError("document too large")
    if not raw:
        return {}
    try:
        parsed = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        raise SyncServerError("body must be JSON") from None
    if not isinstance(parsed, dict):
        raise SyncServerError("body must be a JSON object")
    return parsed


# --------------------------------------------------------------------------- #
# handlers
# --------------------------------------------------------------------------- #
async def health(request: web.Request) -> web.Response:
    return web.json_response(
        {"ok": True, "service": "claunch-sync", "version": __version__}
    )


async def whoami(request: web.Request) -> web.Response:
    user = request["user"]
    return web.json_response({"user": user.name, "namespaces": list(user.namespaces)})


async def list_namespaces(request: web.Request) -> web.Response:
    user = request["user"]
    store: DocStore = request.app["docs"]
    existing = store.namespaces()
    visible = [n for n in existing if user.may_access(n)]
    return web.json_response({"namespaces": visible})


async def get_doc(request: web.Request) -> web.Response:
    store: DocStore = request.app["docs"]
    stored = store.read(request.match_info["namespace"])
    return web.json_response(stored.payload())


async def put_doc(request: web.Request) -> web.Response:
    namespace = request.match_info["namespace"]
    body = await _read_json(request)
    doc = body.get("doc")
    if not isinstance(doc, dict):
        raise SyncServerError("body needs a 'doc' object")
    if "revision" not in body:
        raise SyncServerError(
            "body needs the 'revision' you read (0 if the namespace was empty)"
        )
    try:
        revision = int(body["revision"])
    except (TypeError, ValueError):
        raise SyncServerError("'revision' must be an integer") from None

    store: DocStore = request.app["docs"]
    user = request["user"]
    async with request.app["locks"][namespace]:
        # Serialize per namespace: the revision check and the write have to be
        # one step, or two pushes that read the same revision both succeed.
        stored = store.write(namespace, doc, revision, by=user.name)
    return web.json_response(stored.payload())


async def delete_doc(request: web.Request) -> web.Response:
    store: DocStore = request.app["docs"]
    namespace = request.match_info["namespace"]
    async with request.app["locks"][namespace]:
        deleted = store.delete(namespace)
    return web.json_response({"deleted": deleted, "namespace": namespace})


# --------------------------------------------------------------------------- #
# app
# --------------------------------------------------------------------------- #
def build_app(docs: DocStore, users: UserStore) -> web.Application:
    app = web.Application(
        middlewares=[error_middleware, build_auth_middleware(users)],
        client_max_size=MAX_BODY_BYTES,
    )
    app["docs"] = docs
    app["users"] = users
    app["locks"] = defaultdict(asyncio.Lock)
    app.router.add_get("/api/sync/health", health)
    app.router.add_get("/api/sync/whoami", whoami)
    app.router.add_get("/api/sync/namespaces", list_namespaces)
    app.router.add_get("/api/sync/doc/{namespace}", get_doc)
    app.router.add_put("/api/sync/doc/{namespace}", put_doc)
    app.router.add_delete("/api/sync/doc/{namespace}", delete_doc)
    return app


async def serve(
    docs: DocStore,
    users: UserStore,
    host: str,
    port: int,
    *,
    ready: Optional[asyncio.Event] = None,
) -> int:
    """Run until cancelled (Ctrl-C in the foreground). Returns a process exit code."""
    app = build_app(docs, users)
    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    site = web.TCPSite(runner, host, port, shutdown_timeout=3.0)
    try:
        await site.start()
    except OSError as exc:
        print(f"error: cannot bind {host}:{port}: {exc}")
        await runner.cleanup()
        return 1
    print(f"claunch sync server listening on http://{host}:{port}")
    print(f"documents: {docs.docs_dir}")
    print(f"users:     {users.path}")
    if ready is not None:
        ready.set()
    try:
        await asyncio.Event().wait()  # until cancelled
    except asyncio.CancelledError:
        pass
    finally:
        await runner.cleanup()
    return 0
