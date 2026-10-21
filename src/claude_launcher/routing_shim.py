"""The loopback proxy in front of an API-key provider.

Started on demand by :mod:`claude_launcher.routing` — see that module for why
it exists and how sessions find it. This file is only the transport: forward
everything to the upstream unchanged, except that a JSON object body gains the
configured routing field on the way out (when a spec was given), and every
completion answer (``/v1/messages`` from Claude Code, ``/v1/chat/completions``
from the ``pi`` harness) is watched on its way back so one throughput record
per request lands in :mod:`claude_launcher.metering`'s files. The session a
request belongs to arrives in a private ``X-Claunch-Session`` header, which
is stripped before the upstream sees it.

Run directly with::

    python -m claude_launcher.routing_shim --upstream https://openrouter.ai/api/ \\
        --spec '{"order":["coreweave"],"allow_fallbacks":false}' --port 31500 \\
        --fingerprint abc123

``--spec '{}'`` is the metering-only shim. ``--provider <name>`` makes the
shim read ``providers.<name>.session_affinity`` from the live config on every
request and, while the flag is on, forward the calling session's name (from
the private ``X-Claunch-Session`` header, which is still stripped) as
``x-session-affinity`` for backends that route a session's requests to the
replica holding its prompt cache. The read is live — toggling the flag
(including from the web Settings page) reaches this shim without a restart,
in both directions. A shim started with the legacy ``--session-affinity``
flag and no ``--provider`` keeps that static choice for its whole life.

``--route <name>`` starts the shim for a *dynamic* provider. The shim then
re-reads ``providers.<name>`` on every request — the anthropic endpoint to
forward to, the api key to authenticate with (the session's profile
``set-token`` secret is the fallback below the declared ``api_key``), and
the ``models:`` table that resolves Claude's wire vocabulary (role words
``small``/``large``..., alias words ``haiku``/``opus``..., and Claude's own
``claude-*`` ids) to the backend's model ids. Editing the config — a model
in the table, the endpoint, or the profile's provider selection via
``claunch set-provider`` — reaches running sessions without a relaunch.
Values the table has no answer for pass through unchanged.

Exit codes: ``3`` the port was taken (the caller re-probes and reuses whoever
won it), ``2`` bad arguments.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys

import aiohttp
from aiohttp import web
from multidict import CIMultiDict

from . import metering, routing

#: Header a session-affinity shim forwards the session name under (see
#: :mod:`routing` for why a provider asks for this). Defined there so the
#: spelling has exactly one home; matched case-insensitively like every other
#: header here.
AFFINITY_HEADER = routing.AFFINITY_HEADER

try:
    import truststore as _truststore
except ImportError:  # Python < 3.10, or truststore not installed
    _truststore = None

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


def _affinity_on(app: web.Application) -> bool:
    """Whether to forward the session name as the affinity header right now.

    The shim knows its provider's config name (``--provider``) and reads the
    flag from the live config per request — ``store.load`` serves at most
    100ms-stale state, so a toggle in ``~/.claunch.yaml`` (including from the
    web Settings page) reaches already-running shims without a restart, in
    both directions. A malformed flag is a config error, not a reason to
    break traffic: the header is skipped, the error is logged once per
    distinct message, and the launch-time strictness of
    :func:`routing.session_affinity` surfaces where a human looks.
    """
    cfg = app["cfg"]
    provider = cfg.get("provider")
    if not provider:
        # A shim started by an older build with the static --session-affinity
        # flag: no provider name to re-read, so the spawn-time choice stands.
        return bool(cfg.get("session_affinity"))
    try:
        return routing.session_affinity(provider)
    except routing.RoutingError as exc:
        if app.get("affinity_error") != str(exc):
            print(f"session_affinity config error: {exc}", file=sys.stderr)
            app["affinity_error"] = str(exc)
        return False


def _metered_path(path: str) -> bool:
    """Only completions carry usage; a models listing or a count is noise.

    ``/v1/messages`` is Claude Code (Anthropic Messages), ``/v1/chat/
    completions`` the ``pi`` harness (OpenAI Chat Completions); the meter
    reads both shapes.
    """
    path = path.rstrip("/")
    return path.endswith("/messages") or path.endswith("/chat/completions")


def _inject_auth(headers: CIMultiDict, key: str) -> None:
    """Swap the caller's credential for the route's current one.

    The header the harness used is kept (its scheme included) so a backend
    that expects ``x-api-key`` keeps getting ``x-api-key``; only when no
    auth header arrived at all is a bearer added.
    """
    auth = headers.get("Authorization")
    if auth:
        scheme = auth.split(" ", 1)[0] if " " in auth else "Bearer"
        headers["Authorization"] = f"{scheme} {key}"
    elif headers.get("x-api-key") is not None:
        headers["x-api-key"] = key
    else:
        headers["Authorization"] = f"Bearer {key}"


def _resolve_route(cfg: dict, session_name):
    """``(upstream, auth_key, models_table)`` for a dynamic route, live.

    Raises :class:`routing.RoutingError` when the config has no usable
    answer — the caller turns that into a 502.
    """
    route = cfg["route"]
    upstream = routing.route_upstream(route)
    table = routing.route_models(route)
    auth = routing.route_auth(route, session_name)
    return upstream, auth, table


async def _proxy(request: web.Request) -> web.StreamResponse:
    cfg = request.app["cfg"]
    body = await request.read()
    headers = _forwarded(request.headers)
    # The session name rides in on a private header the daemon added for us;
    # it is ours to read and must not reach the upstream.
    session_name = headers.popall(metering.SESSION_HEADER, [None])[-1]
    upstream = cfg["upstream"]
    table: dict = {}
    if cfg.get("route"):
        try:
            upstream, auth, table = _resolve_route(cfg, session_name)
        except routing.RoutingError as exc:
            return web.json_response(
                {
                    "type": "error",
                    "error": {
                        "type": "api_error",
                        "message": f"claunch routing shim: {exc}",
                    },
                },
                status=502,
            )
        if auth:
            _inject_auth(headers, auth)
    if request.method in ("POST", "PUT", "PATCH") and _is_json(
        request.headers.get("content-type", "")
    ):
        body = routing.merge_body_model(body, cfg["spec"], table)
    if session_name and _affinity_on(request.app):
        # Sticky routing for prompt-cached backends: the session keeps one
        # affinity key for its whole life, so the replica that holds its cache
        # keeps serving it. A caller that set the header itself wins — the
        # same rule merge_body applies to the body.
        if AFFINITY_HEADER not in headers:
            headers[AFFINITY_HEADER] = session_name
    meter = None
    if request.method == "POST" and _metered_path(request.path):
        meter = metering.Meter(session=session_name, path=request.path)
        # Ask for an uncompressed body: the meter reads the usage out of the
        # bytes going past, and a gzip'd answer would be forwarded blind.
        headers.popall("accept-encoding", None)
        headers["accept-encoding"] = "identity"
    live: "metering.LiveBoard" = request.app["live"]
    if meter is not None:
        live.begin(meter)
    try:
        return await _forward(request, cfg, upstream, body, headers, meter)
    finally:
        # Every way out -- an answer, an upstream error, a client that hung up
        # and cancelled this handler -- ends the call in the live view, or the
        # session would read as in flight for as long as the shim lives.
        if meter is not None:
            live.end(meter)


async def _forward(request, cfg, upstream, body, headers, meter) -> web.StreamResponse:
    session: aiohttp.ClientSession = request.app["session"]
    try:
        upstream_resp = await session.request(
            request.method,
            _target(upstream, request),
            headers=headers,
            data=body,
            allow_redirects=False,
        )
    except aiohttp.ClientError as exc:
        if meter is not None:
            meter.status = 502
            _record(request.app, meter, upstream)
        return web.json_response(
            {
                "type": "error",
                "error": {
                    "type": "api_error",
                    "message": f"claunch routing shim could not reach "
                    f"{upstream}: {exc}",
                },
            },
            status=502,
        )
    async with upstream_resp:
        if meter is not None:
            meter.headers(upstream_resp.status, upstream_resp.headers)
        # Streaming, not buffering: an SSE completion must reach the client
        # token by token, exactly as it arrives.
        response = web.StreamResponse(
            status=upstream_resp.status, headers=_forwarded(upstream_resp.headers)
        )
        await response.prepare(request)
        try:
            async for chunk in upstream_resp.content.iter_any():
                if meter is not None:
                    meter.chunk(chunk)
                await response.write(chunk)
        except (aiohttp.ClientError, ConnectionResetError):
            pass  # either end hung up mid-stream; nothing left to say
        if meter is not None:
            _record(request.app, meter, upstream)
        await response.write_eof()
        return response


def _record(app: web.Application, meter: "metering.Meter", upstream: str) -> None:
    cfg = app["cfg"]
    try:
        record = meter.finish()
        metering.append(cfg["fingerprint"], record, upstream=upstream)
        app["live"].end(meter, record)
    except Exception as exc:  # noqa: BLE001 - a record must never break a request
        print(f"metering record failed: {exc}", file=sys.stderr)


async def _live_flusher(app: web.Application):
    """Rewrite the live files a few times a second while a call is in flight."""

    async def loop() -> None:
        board: metering.LiveBoard = app["live"]
        while True:
            await asyncio.sleep(metering.LIVE_FLUSH_S)
            if not board.active():
                continue
            try:
                board.flush()
            except Exception as exc:  # noqa: BLE001 - the live view must never stop the shim
                print(f"metering live flush failed: {exc}", file=sys.stderr)

    task = asyncio.get_running_loop().create_task(loop())
    yield
    task.cancel()
    try:
        await task
    except asyncio.CancelledError:
        pass


async def _health(request: web.Request) -> web.Response:
    cfg = request.app["cfg"]
    return web.json_response(
        {
            "marker": routing.MARKER,
            "fingerprint": cfg["fingerprint"],
            "upstream": cfg["upstream"],
            "spec": cfg["spec"],
            "route": cfg.get("route"),
            "pid": os.getpid(),
        }
    )


async def _shutdown(request: web.Request) -> web.Response:
    # Set the flag *after* this response has had a moment to flush, so the
    # caller learns the shim is going down rather than seeing a dropped socket.
    asyncio.get_running_loop().call_later(0.05, request.app["stop"].set)
    return web.json_response({"stopped": request.app["cfg"]["fingerprint"]})


def _connector() -> "aiohttp.TCPConnector | None":
    """Verify upstream TLS against the OS trust store when ``truststore`` is here.

    Same reason as ``daemon/rag.py``: a corporate TLS-inspection root the OS
    trusts can still fail OpenSSL's own chain checks ("Basic Constraints of CA
    cert not marked critical" was observed on such a machine), and then every
    request through the shim is a 502 while the harness's own Node client,
    validating through the OS, gets through. ``None`` falls back to aiohttp's
    default context.
    """
    if _truststore is None:
        return None
    return aiohttp.TCPConnector(ssl=_truststore.SSLContext(ssl.PROTOCOL_TLS_CLIENT))


async def _client_session(app: web.Application):
    # No total timeout: a streamed completion legitimately runs for minutes.
    # auto_decompress off keeps the body byte-identical to what the upstream
    # sent, so the Content-Encoding we forward stays truthful.
    app["session"] = aiohttp.ClientSession(
        timeout=aiohttp.ClientTimeout(total=None, connect=30, sock_read=None),
        auto_decompress=False,
        connector=_connector(),
    )
    yield
    await app["session"].close()


def build_app(upstream: str, spec: dict, fingerprint: str,
              provider: "str | None" = None,
              session_affinity: bool = False,
              route: "str | None" = None) -> web.Application:
    app = web.Application(client_max_size=1024 * 1024 * 256)
    app["cfg"] = {"upstream": upstream, "spec": spec, "fingerprint": fingerprint,
                  "provider": provider, "session_affinity": session_affinity,
                  "route": route}
    app["live"] = metering.LiveBoard(fingerprint)
    app.cleanup_ctx.append(_client_session)
    app.cleanup_ctx.append(_live_flusher)
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
    parser.add_argument("--provider", default=None,
                        help="config name of the provider this shim fronts; "
                             "the shim reads its session_affinity flag live")
    parser.add_argument("--route", default=None,
                        help="config name of a dynamic provider; the shim "
                             "resolves upstream, credentials and the model "
                             "alias table from the live config per request")
    parser.add_argument("--session-affinity", action="store_true",
                        help="legacy static switch for shims started without "
                             "--provider (kept for running old shims)")
    args = parser.parse_args(argv)
    try:
        spec = json.loads(args.spec)
    except ValueError as exc:
        print(f"bad --spec: {exc}", file=sys.stderr)
        return 2
    if not isinstance(spec, dict):
        print("--spec must be a JSON object ({} for metering only)", file=sys.stderr)
        return 2
    app = build_app(args.upstream, spec, args.fingerprint,
                    provider=args.provider, session_affinity=args.session_affinity,
                    route=args.route)
    return asyncio.run(serve(app, args.port))


if __name__ == "__main__":  # pragma: no cover - process entry point
    sys.exit(main())
