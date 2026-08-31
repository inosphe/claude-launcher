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
from typing import Optional

from aiohttp import web

from .. import daemon_client, store
from . import cflow_clock, paths, restart_notice, resume, runtime_state, window as window_mod
from . import session_reminder
from .api import build_app, notify_shutdown
from .manager import SessionManager
from .mesh import MeshError, MeshManager

log = logging.getLogger("claunch.daemon")

#: What ``_serve`` returns when the shutdown it drained was a restart request
#: (``POST /api/daemon/restart``): ``main`` spawns the successor only after
#: releasing the singleton lock, so the new daemon finds it free instead of
#: spending its grace window waiting this process out. Never a process exit
#: code — the restarting daemon itself still exits 0.
RESTART_CODE = 75


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


def _quiet_reset_errors(loop: asyncio.AbstractEventLoop) -> None:
    """Stop a peer's abrupt disconnect from logging as a daemon error.

    On Windows the proactor closes a socket by calling ``shutdown()`` on it
    from a callback; when the far end already sent a reset (WinError 10054 —
    a closed browser tab, a killed ``claunch attach``) that call raises inside
    asyncio itself, with no coroutine to receive it, and the default handler
    reports it at ERROR. Nothing is wrong and nothing can be done about it
    from here — the connection is over either way — so a connection reset with
    no task behind it is demoted to debug and everything else keeps the
    default handling.
    """
    default = loop.get_exception_handler()

    def handler(loop_: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError) and context.get("future") is None:
            log.debug("connection reset by peer: %s", context.get("message"))
            return
        if default is None:
            loop_.default_exception_handler(context)
        else:
            default(loop_, context)

    loop.set_exception_handler(handler)


async def _serve(host: str, port: int, cfg: dict, bound: Optional[dict] = None) -> int:
    _quiet_reset_errors(asyncio.get_running_loop())
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
        gate_timeout=float(cfg["restart_approval_timeout"]),
        goto_timeout=float(cfg["goto_approval_timeout"]),
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
    # The snapshot is taken here because here is the boot: what this process
    # serves is the content of its source directory at import, and that stops
    # being readable as soon as anyone edits or checks out anything.
    # tools/deploy_check.py is the reader.
    runtime_state.write_daemon_json(
        host, actual_port, code=runtime_state.code_snapshot()
    )
    if bound is not None:
        bound["port"] = actual_port
    log.info("listening on http://%s:%s", host, actual_port)
    # Now that this daemon is announced, settle the account of the boot: read
    # (and clear) whatever asked for it, append this boot to the ledger the
    # next one will compare against, and turn both into debts. Done here
    # rather than at delivery time because the requests file is the *previous*
    # daemon's epitaph -- anything that appends to it after this point belongs
    # to the next restart, not this one.
    boot = runtime_state.read_daemon_json() or {}
    debts = restart_notice.note_boot(
        pid=boot.get("pid") or os.getpid(),
        started_at=boot.get("started_at") or "",
        version=boot.get("version") or "",
        port=actual_port,
        restored=list(manager.resumed_busy),
    )
    if debts:
        log.info(
            "restart notice: %d owed by this boot (%s)",
            len(debts),
            ", ".join(sorted({d["kind"] for d in debts})),
        )

    uplink, uplink_task = _start_uplink(actual_port)
    relay_state["uplink"] = uplink
    if uplink is not None:
        _wire_federation(mesh_manager, uplink)
    mesh_manager.start()
    ask_clock = cflow_clock.AskClock()
    ask_clock.start()
    reminder_clock = session_reminder.SessionReminderService(manager, mesh_manager)
    reminder_clock.start()
    window_reminder_clock = window_mod.WindowReminderClock(manager, app["window"])
    window_reminder_clock.start()
    ping_clock = cflow_clock.StallPingClock(manager)
    ping_clock.start()
    window_clock = cflow_clock.WindowClock(manager)
    window_clock.start()
    timer_clock = cflow_clock.TimerClock(manager)
    timer_clock.start()
    checklist_clock = cflow_clock.ChecklistClock(manager)
    checklist_clock.start()
    round_clock = cflow_clock.RoundStartClock(manager)
    round_clock.start()
    event_clock = cflow_clock.RunEventClock(manager, mesh_manager)
    event_clock.start()
    # Published so the dashboard can report what these two are holding. Only
    # the two that type into a driving session on a timer: those are the ones
    # a person watching a terminal has no way to see coming, and the ones
    # whose silence is ambiguous — configured-and-armed and
    # configured-but-dead look identical from outside.
    app["session_reminder"] = reminder_clock
    # The dashboard's cflow timer readout consumes the cflow source's proxy
    # surface from the session-level service.  Keep this key for the existing
    # API while the runtime owner is published above under its real name.
    app["cflow_clocks"] = {"reminder": reminder_clock, "ping": ping_clock}
    # Last, and only now: the sessions restore brought back are alive but
    # nothing is driving them. Started after the server is up because a nudge
    # can send an agent straight back to the API it was using.
    resume_nudge = resume.ResumeNudge(
        manager,
        manager.resumed_busy,
        # The sessions whose conversation was not there to reopen. They get a
        # different message and the re-briefing, which is why the mesh manager
        # comes along — it is half of what a re-briefing is made of.
        blank=manager.resumed_blank,
        mesh_mgr=mesh_manager,
    )
    resume_nudge.start()
    # Started alongside it, and deliberately not merged into it: the nudge is
    # allowed to give up on a session that is working again, and this is not
    # (see restart_notice's module docstring). Where both are owed, whichever
    # lands first makes the other redundant -- and if that is this one, the
    # nudge reads the session as driven and stands down, which is right.
    restart_notice_task = restart_notice.RestartNotice(manager)
    restart_notice_task.start()

    try:
        await app["shutdown_event"].wait()
        log.info(
            "shutdown requested%s",
            " (restart)" if app["restart_requested"] else "",
        )
    except asyncio.CancelledError:
        log.info("cancelled; shutting down")
    finally:
        # Announce before anything is torn down: attached CLIs must learn this
        # is a daemon stop/restart (reattach later) before shutdown_all makes
        # their sessions look like programs that exited on their own.
        await notify_shutdown(app)
        if uplink is not None:
            uplink.stop()
        if uplink_task is not None:
            uplink_task.cancel()
            try:
                await uplink_task
            except (asyncio.CancelledError, Exception):
                pass
        await resume_nudge.shutdown()
        # Undelivered debts stay on disk; shutdown only stops offering them.
        await restart_notice_task.shutdown()
        # Pending wind-downs are dropped, not finished: shutdown_all below
        # ends every session the daemon's way, and they come back on restart.
        await app["beads"].cancel_all()
        await checklist_clock.shutdown()
        await event_clock.shutdown()
        await ping_clock.shutdown()
        await window_reminder_clock.shutdown()
        await reminder_clock.shutdown()
        await ask_clock.shutdown()
        await mesh_manager.shutdown()
        runtime_state.remove_daemon_json()
        await manager.shutdown_all()
        await runner.cleanup()
    return RESTART_CODE if app["restart_requested"] else 0


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
    bound: dict = {}
    try:
        code = asyncio.run(_serve(host, port, cfg, bound))
    except KeyboardInterrupt:
        code = 0
    finally:
        lock.release()
    if code == RESTART_CODE:
        log.info("spawning successor daemon")
        daemon_client.spawn_daemon(_successor_env(bound.get("port")))
        return 0
    return code


def _successor_env(actual_port: Optional[int]) -> Optional[dict]:
    """The environment for the successor, or ``None`` to inherit ours.

    Its one job is pinning the successor to the port this daemon was
    serving on. Only named instances need it, and only they are affected:
    the default daemon's port is fixed in the config, so its successor
    rebinds the same one anyway, while an instance binds an ephemeral port
    and would come back somewhere else. That matters because the thing most
    likely to have asked for the restart is a browser on this address — a
    successor that moves is one the page cannot follow. An explicitly
    pinned port (``CLAUNCH_DAEMON_PORT`` already set) is left as it is.

    Returned as a copy rather than set on ``os.environ``: a variable poked
    into this process on the way out is still there for everything else
    sharing it, which in a test run is every later test.
    """
    if not actual_port or not paths.instance():
        return None
    if os.environ.get("CLAUNCH_DAEMON_PORT"):
        return None
    return {**os.environ, "CLAUNCH_DAEMON_PORT": str(actual_port)}


if __name__ == "__main__":
    sys.exit(main())
