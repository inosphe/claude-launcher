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

from .. import store
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
    site = web.TCPSite(runner, host, port)
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

    try:
        await app["shutdown_event"].wait()
        log.info("shutdown requested")
    except asyncio.CancelledError:
        log.info("cancelled; shutting down")
    finally:
        runtime_state.remove_daemon_json()
        await manager.shutdown_all()
        await runner.cleanup()
    return 0


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
    if not lock.acquire():
        log.info("another daemon already holds the lock; exiting")
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
