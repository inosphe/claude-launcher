"""Bring the fleet back when the PTY host goes away under a running daemon
(claunch-hor3b.1).

Without the host, a daemon restart ends every session and the next boot's
``restore_all`` relaunches each one with ``--resume``. With it, the sessions
live in the host, and when the host itself ended -- a SIGTERM, a lost
connection -- every session went with it while the daemon stayed up: the
exits were filed as failures, and nothing brought them back
(2026-10-28: 160 sessions ``failed(-9)``, see claunch-hor3b).

This watch gives that case the restart's answer. The manager files a session
the host took down as one still to be restored
(:meth:`SessionManager._ended_with_host`); when the connection closes, the
watch installs a new host -- or none, in which case sessions spawn in the
daemon as they do with ``pty_host`` off -- waits for the old connection's
sessions to finish exiting, and relaunches them
(:meth:`SessionManager.relaunch_host_ended`).
"""

from __future__ import annotations

import asyncio
import logging
import time
from typing import Callable, Optional

from . import pty_backend

log = logging.getLogger("claunch.daemon.host_watch")

#: Seconds between looks at the installed host's connection.
POLL_INTERVAL = 1.0
#: Seconds to wait for the sessions of a lost connection to finish exiting
#: before relaunching the ones that have.
EXIT_SETTLE = 10.0


async def watch(manager, connect: Callable[[], Optional[object]], *,
                interval: float = POLL_INTERVAL, settle: float = EXIT_SETTLE) -> None:
    """Run until the daemon shuts down or no host is installed.

    ``connect`` starts or reaches a host and installs nothing itself: it
    returns the client, or None when no host could be had.
    """
    while True:
        client = pty_backend.installed_host()
        if client is None:
            return
        while not client.closed.is_set():
            await asyncio.sleep(interval)
        if manager.shutting_down:
            return
        log.warning("the PTY host connection closed under a running daemon; "
                    "starting a new host and relaunching the sessions it ended")
        deadline = time.monotonic() + settle
        while manager.bound_to_host(client) and time.monotonic() < deadline:
            await asyncio.sleep(0.1)
        try:
            new = await asyncio.to_thread(connect)
        except Exception:
            log.exception("could not start a new PTY host; spawning in the daemon")
            new = None
        if manager.shutting_down:
            if new is not None:
                new.close()
            return
        pty_backend.use_host(new)
        if new is None:
            log.warning("no PTY host: the sessions relaunch in the daemon")
        await manager.relaunch_host_ended()
