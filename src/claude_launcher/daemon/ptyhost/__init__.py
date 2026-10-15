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

Stage 1 added the host, its protocol and :class:`client.RemotePty`, behind
the daemon setting ``pty_host`` (off by default). Stage 2 (claunch-xnnct.1)
keeps the sessions when the client goes away: a daemon restart detaches them
(``SessionManager.shutdown_all(mode="keep")``) and the next daemon adopts them
(``restore_all``), so the children keep their pids and nothing is resumed. A
restart with ``--with-pty-host`` (mode ``full``) and ``daemon stop`` end the
sessions and the host as before.

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
from typing import Dict, List, Optional

#: Version of the wire protocol. The hello carries the range a client
#: speaks; the host refuses a client whose range does not include this.
#: 2: sessions outlive the client; ``attach``, ``ack_exit`` and ``end_all``.
PROTO_VERSION = 2

#: ``sun_path`` is 108 bytes on Linux and 104 on macOS; leave room.
_SUN_PATH_MAX = 100

#: Environment variable naming who a host belongs to. :func:`client.start_host`
#: hands it on from the starting process's environment, so a host started by a
#: daemon subprocess carries it too; tests/conftest.py sets it to the test
#: run's id and ends (and reports) every host under a test's directories that
#: carries that id when the test is over.
OWNER_ENV = "CLAUNCH_PTYHOST_OWNER"


def code_files(root: Optional[Path] = None) -> Dict[str, str]:
    """The SHA-256 of each file the host runs, keyed by its path under
    ``daemon/`` (claunch-xnnct.3): every ``ptyhost/*.py`` and
    ``pty_backend.py``.

    ``root`` is the ``daemon`` package directory; the default is the one this
    module was imported from, which is the code an editable install runs.
    A file that cannot be read is left out, so the id still changes.
    """
    root = Path(root) if root is not None else Path(__file__).resolve().parent.parent
    names = sorted(p.name for p in (root / "ptyhost").glob("*.py"))
    paths = [f"ptyhost/{n}" for n in names] + ["pty_backend.py"]
    out: Dict[str, str] = {}
    for rel in paths:
        try:
            out[rel] = hashlib.sha256((root / rel).read_bytes()).hexdigest()
        except OSError:
            continue
    return out


def code_id(files: Dict[str, str]) -> str:
    """One short id for :func:`code_files`: ``p<PROTO_VERSION>-<16 hex>``.

    Two hosts with the same id run the same bytes; the protocol version is
    in front so a reader can tell a protocol change from a code change.
    """
    h = hashlib.sha256()
    for rel in sorted(files):
        h.update(rel.encode("utf-8") + b"\0" + files[rel].encode("ascii") + b"\n")
    return f"p{PROTO_VERSION}-{h.hexdigest()[:16]}"


def changed_files(a: Dict[str, str], b: Dict[str, str]) -> List[str]:
    """The paths whose digest differs between two :func:`code_files` maps
    (present in one only counts as differing), sorted."""
    return sorted(rel for rel in set(a) | set(b) if a.get(rel) != b.get(rel))


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
