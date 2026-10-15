"""Which code the PTY host runs, compared with the daemon's (claunch-xnnct.3).

A keep restart (claunch-xnnct.1) leaves the PTY host running, so a change to
``daemon/ptyhost/`` or ``daemon/pty_backend.py`` reaches the daemon and not
the host: the host keeps the bytes it read at its start. The host reports a
code id in its hello reply (:func:`ptyhost.code_id`), and this module holds
the two comparisons made with it:

* :func:`info` -- the host against the code this daemon process loaded. A
  mismatch means the host is *stale*: it runs older (or other) code than the
  daemon talking to it. ``GET /api/daemon`` carries it as ``pty_host``, and
  the dashboard, the sidebar badge and ``claunch daemon status`` show it.
* :func:`keep_check` -- the host against the code on disk now, which is what
  the next daemon will load. A keep restart with a mismatch is refused unless
  the caller says ``keep_stale``; ``--with-pty-host`` is the restart that
  replaces the host.

This module is not one of the hashed files and is never imported by the
host, so changing it does not make a host stale.
"""

from __future__ import annotations

from typing import Dict, Optional

from . import pty_backend
from .ptyhost import changed_files, code_files, code_id

#: The daemon setting ``pty_host`` as this daemon read it at start;
#: :func:`configure` sets it. With the setting on and no host connected the
#: state is ``unreachable`` (the daemon spawns in-process, claunch-xnnct.1).
_configured = False

#: :func:`code_files` as of this daemon's start (see :func:`loaded`).
_loaded: Optional[Dict[str, str]] = None

#: How many changed paths a refusal names.
CHANGED_SHOWN = 5

#: The refusal's hint, shared by the API and the CLI.
HINT = (
    "claunch daemon restart --with-pty-host   # ends the sessions and relaunches them on a new host\n"
    "claunch daemon restart --keep-stale-host # keeps the sessions on the old host anyway"
)


def configure(configured: bool) -> None:
    """Record the ``pty_host`` setting and the code this daemon loaded.

    Called once at daemon start, before the first comparison, so
    :func:`loaded` reads the files as they were when this process imported
    them and not as a later checkout left them.
    """
    global _configured
    _configured = bool(configured)
    loaded()


def loaded() -> Dict[str, str]:
    """The host files as this daemon loaded them (read at the first call)."""
    global _loaded
    if _loaded is None:
        _loaded = code_files()
    return _loaded


def info() -> dict:
    """The PTY host as ``GET /api/daemon`` reports it.

    ``state`` is one of ``off`` (setting off: sessions run in the daemon),
    ``connected``, ``stale`` (the host's code id differs from the daemon's)
    and ``unreachable`` (setting on, no host connected: sessions spawn in
    the daemon). ``started_at`` is the host's start time (epoch seconds) so
    a reader computes its uptime when it shows it.
    """
    daemon_code = code_id(loaded())
    host = pty_backend.current_host()
    if host is None:
        return {
            "configured": _configured,
            "state": "unreachable" if _configured else "off",
            "pid": pty_backend.stranded_host(),
            "proto": None,
            "code": None,
            "daemon_code": daemon_code,
            "stale": False,
            "started_at": None,
            "sessions": 0,
        }
    stale = host.host_code != daemon_code
    return {
        "configured": _configured,
        "state": "stale" if stale else "connected",
        "pid": host.host_pid,
        "proto": host.host_proto,
        "code": host.host_code,
        "daemon_code": daemon_code,
        "stale": stale,
        "started_at": host.host_started_at,
        "sessions": host.session_count(),
    }


def keep_check() -> dict:
    """Whether a keep restart would leave the host on code other than what
    the next daemon loads.

    ``stale`` is False when no host is connected: a keep restart then keeps
    nothing. ``changed`` names up to :data:`CHANGED_SHOWN` paths; a host that
    reports no code id has no file list, and every path counts as changed.
    """
    host = pty_backend.current_host()
    if host is None:
        return {"stale": False, "pid": None, "host_code": None, "disk_code": None,
                "changed": [], "more": 0}
    disk = code_files()
    disk_code = code_id(disk)
    stale = host.host_code != disk_code
    changed = changed_files(host.host_code_files, disk) if stale else []
    return {
        "stale": stale,
        "pid": host.host_pid,
        "host_code": host.host_code,
        "disk_code": disk_code,
        "changed": changed[:CHANGED_SHOWN],
        "more": max(0, len(changed) - CHANGED_SHOWN),
    }


def refusal(check: dict) -> str:
    """The message a refused keep restart prints, from :func:`keep_check`."""
    files = ", ".join(check.get("changed") or []) or "unknown files"
    if check.get("more"):
        files += f" and {check['more']} more"
    return (
        f"refused: the PTY host (pid {check.get('pid')}) runs code that differs from "
        f"what the restarted daemon will load ({files}). A keep restart leaves it "
        "running the old code."
    )
