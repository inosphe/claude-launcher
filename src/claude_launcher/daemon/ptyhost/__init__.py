"""The PTY host: a small separate process that owns the session PTYs (claunch-xnnct).

The daemon is the parent of every harness child today, so a daemon restart
ends every session and the next daemon brings each one back with
``--resume``. The PTY host is the process that holds the PTY handles and the
children instead; the daemon talks to it over a local socket and keeps
everything else (rendering, status, deliveries, clocks, the API).

This package is imported by the host process itself, so it uses the standard
library and :mod:`claude_launcher.daemon.pty_backend` only, and imports what it
needs at start: an installed copy can be replaced on disk under a running
host, and a late import would then mix two versions of the code.

Stage 1 (this module set): the host, its protocol and :class:`client.RemotePty`,
behind the daemon setting ``pty_host`` (off by default). A client that goes
away takes its sessions with it, which is what a daemon exit does today --
surviving a restart is stage 2 (see the design comment on claunch-xnnct).

Files, all under one directory (the daemon's ``ptyhost/``):
``ptyhost.sock`` (or a short path under the temp dir, see :func:`socket_path`),
``ptyhost.token`` (0600, written by the host at start), ``ptyhost.pid``,
``ptyhost.log`` (the host's stderr).
"""

from __future__ import annotations

import hashlib
import os
import tempfile
from pathlib import Path

#: Version of the wire protocol. The hello carries the range a client
#: speaks; the host refuses a client whose range does not include this.
PROTO_VERSION = 1

#: ``sun_path`` is 108 bytes on Linux and 104 on macOS; leave room.
_SUN_PATH_MAX = 100

#: Environment variable a host inherits from whoever started it, so a test
#: run can find (and end) the hosts it started -- see tests/conftest.py.
OWNER_ENV = "CLAUNCH_PTYHOST_OWNER"


def socket_path(directory: Path) -> Path:
    """Where the host for ``directory`` listens.

    Inside the directory when the path fits ``sun_path``; otherwise a name in
    the temp directory derived from the directory's own path, so two homes
    never share one and the client computes the same name the host did.
    """
    directory = Path(directory)
    inside = directory / "ptyhost.sock"
    if len(os.fsencode(str(inside))) <= _SUN_PATH_MAX:
        return inside
    digest = hashlib.sha256(os.fsencode(str(directory.resolve()))).hexdigest()[:16]
    uid = os.getuid() if hasattr(os, "getuid") else 0
    return Path(tempfile.gettempdir()) / f"claunch-ptyhost-{uid}-{digest}.sock"


def token_path(directory: Path) -> Path:
    return Path(directory) / "ptyhost.token"


def pid_path(directory: Path) -> Path:
    return Path(directory) / "ptyhost.pid"


def log_path(directory: Path) -> Path:
    return Path(directory) / "ptyhost.log"
