"""Daemon process entrypoint: ``python -m claude_launcher.daemon``.

Started detached by the CLI's auto-start (or ``claunch daemon start``); runs
until ``POST /api/daemon/shutdown`` (or SIGINT when run in the foreground with
``--foreground`` for debugging).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time

from aiohttp import web

from .. import daemon_client, store
from . import paths, runtime_state
from .api import build_app
from .manager import SessionManager
from .mesh import MeshError, MeshManager

log = logging.getLogger("claunch.daemon")


def _setup_logging(foreground: bool) -> None:
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        paths.daemon_dir().mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(paths.log_file(), encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers if foreground else handlers[1:] or handlers,
    )


async def _serve(host: str, port: int, cfg: dict) -> int:
    manager = SessionManager(
        idle_threshold=float(cfg["idle_threshold"]),
        scrollback=int(cfg["scrollback_lines"]),
        restore_default=bool(cfg["restore"]),
    )
    failed = manager.restore_all()
    for name in failed:
        log.warning("failed to restore session %r", name)
    restored = [s.sdef.name for s in manager.list() if not s.exited]
    retired = [s.sdef.name for s in manager.list() if s.exited]
    if restored:
        log.info("restored sessions: %s", ", ".join(restored))
    if retired:
        log.info(
            "kept %d exited session record(s), respawnable: %s",
            len(retired),
            ", ".join(retired),
        )

    mesh_manager = MeshManager(manager)
    mesh_manager.load_all()

    token = runtime_state.load_or_create_token()
    relay_state = {"uplink": None}

    def _relay_state() -> dict:
        uplink = relay_state["uplink"]
        if uplink is None:
            return {"configured": False, "connected": False, "name": None}
        return {
            "configured": True,
            "connected": uplink.connected,
            "name": uplink.name,
            "url": uplink.url,
        }

    app = build_app(
        manager,
        token,
        started_at=time.monotonic(),
        mesh=mesh_manager,
        relay_state=_relay_state,
    )

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    # Default shutdown_timeout is 60s per lingering connection — far longer
    # than the restart flow's patience (stop waits 10s, the successor's lock
    # grace is 15s). Keep teardown well inside that budget.
    site = web.TCPSite(runner, host, port, shutdown_timeout=3.0)
    try:
        await site.start()
    except OSError as exc:
        log.error("cannot bind %s:%s: %s", host, port, exc)
        await runner.cleanup()
        return 1

    actual_port = port
    server = getattr(site, "_server", None)
    if server is not None and server.sockets:
        actual_port = server.sockets[0].getsockname()[1]
    runtime_state.write_daemon_json(host, actual_port)
    log.info("listening on http://%s:%s", host, actual_port)

    uplink, uplink_task = _start_uplink(actual_port)
    relay_state["uplink"] = uplink
    if uplink is not None:
        _wire_federation(mesh_manager, uplink)
    mesh_manager.start()

    try:
        await app["shutdown_event"].wait()
        log.info("shutdown requested")
    except asyncio.CancelledError:
        log.info("cancelled; shutting down")
    finally:
        if uplink is not None:
            uplink.stop()
        if uplink_task is not None:
            uplink_task.cancel()
            try:
                await uplink_task
            except (asyncio.CancelledError, Exception):
                pass
        await mesh_manager.shutdown()
        runtime_state.remove_daemon_json()
        await manager.shutdown_all()
        await runner.cleanup()
    return 0


def _acquire_with_grace(
    lock: runtime_state.SingletonLock, *, timeout: float = 15.0, poll: float = 0.2
) -> bool:
    """Acquire the singleton lock, waiting out a predecessor that is draining.

    A restart stops the old daemon and spawns the new one right away, but the
    old process keeps holding the lock while its sessions shut down. Retry for
    a grace window instead of losing that race — while still exiting fast in
    the plain double-start case, where the lock holder is actually serving.
    """
    deadline = time.monotonic() + timeout
    while True:
        if lock.acquire():
            return True
        if daemon_client.is_serving():
            return False
        if time.monotonic() >= deadline:
            return False
        time.sleep(poll)


def _start_uplink(actual_port: int):
    """Start the relay uplink task if a ``daemon.relay`` block is configured.

    The uplink always dials the loopback address so the tunnel can't widen the
    daemon's own network exposure, regardless of the daemon's bind host.
    """
    from . import relay_uplink

    cfg = store.relay_config()
    # A named instance sharing the config file with its siblings must not also
    # share their relay identity — suffix the default backend name so every
    # instance registers under its own directory entry.
    if paths.instance() and not (os.environ.get("CLAUNCH_RELAY_NAME") or cfg.get("name")):
        import socket

        cfg["name"] = f"{socket.gethostname()}-{paths.instance()}"
    uplink = relay_uplink.config_from_env_and_dict(
        cfg, local_host="127.0.0.1", local_port=actual_port
    )
    if uplink is None:
        return None, None
    log.info("starting relay uplink → %s (backend %r)", uplink.url, uplink.name)
    return uplink, asyncio.ensure_future(uplink.run())


def _wire_federation(mesh_manager: MeshManager, uplink) -> None:
    """Give the mesh manager a peer transport riding the relay uplink.

    The transport is one JSON POST per call, bridged to the peer daemon's
    ``/peer/*`` endpoint through the relay (PEER_OPEN). Transport-level
    failures surface as PeerUnreachable (the mirror may then queue durably);
    an HTTP-level rejection surfaces as plain MeshError (never queued).
    """
    from . import peer_client, relay_uplink
    from .mesh import PeerUnreachable

    async def peer_call(machine: str, path: str, body: dict) -> dict:
        raw = peer_client.build_request(path, body, host=machine)
        try:
            resp = await uplink.peer_http(machine, raw)
        except relay_uplink.PeerError as exc:
            raise PeerUnreachable(str(exc)) from None
        try:
            status, payload = peer_client.parse_response(resp)
        except peer_client.PeerHttpError as exc:
            raise PeerUnreachable(f"peer {machine!r}: {exc}") from None
        if status >= 400:
            detail = payload.get("error") or f"HTTP {status}"
            raise MeshError(f"peer {machine!r} rejected {path}: {detail}")
        return payload

    mesh_manager.machine = uplink.name
    mesh_manager.peer_transport = peer_call
    mesh_manager.relay_connected = lambda: uplink.connected
    mesh_manager.peer_lister = uplink.peer_list


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="claunch-daemon")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="log to stderr too (for debugging; the CLI starts the daemon detached)",
    )
    parser.add_argument(
        "--name",
        metavar="NAME",
        help="run as the named daemon instance (tmux -L style; also settable "
        "via the CLAUNCH_DAEMON env var)",
    )
    args = parser.parse_args(argv)
    if args.name:
        # Into the environment (not a variable) so every paths.instance()
        # call — and any child process — sees the same instance.
        os.environ[paths.INSTANCE_ENV] = paths.validate_instance(args.name)
    _setup_logging(args.foreground)

    lock = runtime_state.SingletonLock()
    if not _acquire_with_grace(lock):
        log.info(
            "another daemon holds the lock (already serving, or a predecessor "
            "did not exit in time); exiting"
        )
        return 0

    cfg = store.daemon_config()
    host = str(cfg["host"])
    port = int(cfg["port"])
    if paths.instance():
        # Named instances share the config file with the default daemon, so
        # its fixed port would collide. They bind an ephemeral port instead
        # (their daemon.json is the discovery channel) unless one is pinned.
        port = int(os.environ.get("CLAUNCH_DAEMON_PORT") or 0)
        log.info("daemon instance %r (state: %s)", paths.instance(), paths.daemon_dir())
    try:
        return asyncio.run(_serve(host, port, cfg))
    except KeyboardInterrupt:
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
