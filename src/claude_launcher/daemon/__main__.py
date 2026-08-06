"""Daemon process entrypoint: ``python -m claude_launcher.daemon``.

Started detached by the CLI's auto-start (or ``claunch daemon start``); runs
until ``POST /api/daemon/shutdown`` (or SIGINT when run in the foreground with
``--foreground`` for debugging).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import sys
import time

from aiohttp import web

from .. import daemon_client, store
from . import paths, runtime_state
from .api import build_app
from .manager import SessionManager

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
    restored = [s.sdef.name for s in manager.list()]
    if restored:
        log.info("restored sessions: %s", ", ".join(restored))

    token = runtime_state.load_or_create_token()
    app = build_app(manager, token, started_at=time.monotonic())

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

    uplink = relay_uplink.config_from_env_and_dict(
        store.relay_config(), local_host="127.0.0.1", local_port=actual_port
    )
    if uplink is None:
        return None, None
    log.info("starting relay uplink → %s (backend %r)", uplink.url, uplink.name)
    return uplink, asyncio.ensure_future(uplink.run())


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="claunch-daemon")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="log to stderr too (for debugging; the CLI starts the daemon detached)",
    )
    args = parser.parse_args(argv)
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
    try:
        return asyncio.run(_serve(host, port, cfg))
    except KeyboardInterrupt:
        return 0
    finally:
        lock.release()


if __name__ == "__main__":
    sys.exit(main())
