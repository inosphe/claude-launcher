"""The loopback proxy that puts a routing spec into every JSON request body.

Started on demand by :mod:`claude_launcher.routing` — see that module for why
it exists and how sessions find it. This file is only the transport: forward
everything to the upstream unchanged, except that a JSON object body gains the
configured routing field on the way out.

Run directly with::

    python -m claude_launcher.routing_shim --upstream https://openrouter.ai/api/ \\
        --spec '{"order":["coreweave"],"allow_fallbacks":false}' --port 31500 \\
        --fingerprint abc123

Exit codes: ``3`` the port was taken (the caller re-probes and reuses whoever
won it), ``2`` bad arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import sys

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from . import routing

#: Headers that describe *this* hop and must not be forwarded to the next one.
#: ``content-length`` is here because the body length changes when the spec is
#: merged in, and ``host`` because it would still name the loopback shim.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
        "host",
        "content-length",
        "expect",
    }
)


def _forwarded(headers) -> CIMultiDict:
    """Every header worth passing on, repeats included (``Set-Cookie``...)."""
    out: CIMultiDict = CIMultiDict()
    for key, value in headers.items():
        if key.lower() not in HOP_BY_HOP:
            out.add(key, value)
    return out


def _target(upstream: str, request: web.Request) -> str:
    """Upstream URL for a request that arrived at the shim.

    ``upstream`` may carry a path prefix of its own (``https://host/api/``);
    the request path is appended to it, so ``/v1/messages`` becomes
    ``https://host/api/v1/messages``.
    """
    url = upstream.rstrip("/") + request.rel_url.raw_path
    query = request.rel_url.raw_query_string
    return f"{url}?{query}" if query else url


def _is_json(content_type: str) -> bool:
    return "json" in content_type.lower()


async def _proxy(request: web.Request) -> web.StreamResponse:
    cfg = request.app["cfg"]
    body = await request.read()
    if request.method in ("POST", "PUT", "PATCH") and _is_json(
        request.headers.get("content-type", "")
    ):
        body = routing.merge_body(body, cfg["spec"])
    session: aiohttp.ClientSession = request.app["session"]
    try:
        upstream = await session.request(
            request.method,
            _target(cfg["upstream"], request),
            headers=_forwarded(request.headers),
            data=body,
            allow_redirects=False,
        )
    except aiohttp.ClientError as exc:
        return web.json_response(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": f"claunch routing shim could not reach "
                    f"{cfg['upstream']}: {exc}",
                },
            },
            status=502,
        )
    async with upstream:
        # Streaming, not buffering: an SSE completion must reach the client
        # token by token, exactly as it arrives.
        response = web.StreamResponse(
            status=upstream.status, headers=_forwarded(upstream.headers)
        )
        await response.prepare(request)
        try:
            async for chunk in upstream.content.iter_any():
                await response.write(chunk)
        except (aiohttp.ClientError, ConnectionResetError):
            pass  # either end hung up mid-stream; nothing left to say
        await response.write_eof()
        return response


async def _health(request: web.Request) -> web.Response:
    cfg = request.app["cfg"]
    return web.json_response(
        {
            "marker": routing.MARKER,
            "fingerprint": cfg["fingerprint"],
            "upstream": cfg["upstream"],
            "spec": cfg["spec"],
            "pid": os.getpid(),
        }
    )


async def _shutdown(request: web.Request) -> web.Response:
    # Set the flag *after* this response has had a moment to flush, so the
    # caller learns the shim is going down rather than seeing a dropped socket.
    asyncio.get_running_loop().call_later(0.05, request.app["stop"].set)
    return web.json_response({"stopped": request.app["cfg"]["fingerprint"]})


async def _client_session(app: web.Application):
    # No total timeout: a streamed completion legitimately runs for minutes.
    # auto_decompress off keeps the body byte-identical to what the upstream
    # sent, so the Content-Encoding we forward stays truthful.
    app["session"] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, connect=30, sock_read=None),
        auto_decompress=False,
    )
    yield
    await app["session"].close()


def build_app(upstream: str, spec: dict, fingerprint: str) -> web.Application:
    app = web.Application(client_max_size=1024 * 1024 * 256)
    app["cfg"] = {"upstream": upstream, "spec": spec, "fingerprint": fingerprint}
    app.cleanup_ctx.append(_client_session)
    app.router.add_get(routing.HEALTH_PATH, _health)
    app.router.add_post(routing.SHUTDOWN_PATH, _shutdown)
    app.router.add_route("*", "/{tail:.*}", _proxy)
    return app


async def serve(app: web.Application, port: int) -> int:
    """Run ``app`` on loopback until something asks it to stop.

    Not :func:`aiohttp.web.run_app`: that one only unwinds on a signal, and the
    shutdown endpoint has to be able to end the process from inside a handler.
    Loopback only — the shim carries no credentials of its own (it forwards the
    caller's) but an open relay to a paid API is nobody's idea of a good time.
    """
    app["stop"] = asyncio.Event()
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "127.0.0.1", port)
    try:
        await site.start()
    except OSError as exc:
        # Almost always a start race: another launch got this port first. The
        # caller re-probes and reuses the winner, so this is not an incident.
        print(f"could not bind 127.0.0.1:{port}: {exc}", file=sys.stderr)
        await runner.cleanup()
        return 3
    try:
        await app["stop"].wait()
    finally:
        await runner.cleanup()
    return 0


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="claude_launcher.routing_shim")
    parser.add_argument("--upstream", required=True)
    parser.add_argument("--spec", required=True, help="routing spec as JSON")
    parser.add_argument("--port", type=int, required=True)
    parser.add_argument("--fingerprint", required=True)
    args = parser.parse_args(argv)
    try:
        spec = json.loads(args.spec)
    except ValueError as exc:
        print(f"bad --spec: {exc}", file=sys.stderr)
        return 2
    if not isinstance(spec, dict) or not spec:
        print("--spec must be a non-empty JSON object", file=sys.stderr)
        return 2
    app = build_app(args.upstream, spec, args.fingerprint)
    return asyncio.run(serve(app, args.port))


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
