"""Filesystem locations for daemon runtime state.

Everything machine-local the daemon needs (its address file, auth token, lock,
session definitions, per-session logs) lives under ``<launcher home>/daemon/``.
None of this belongs in ``~/.claunch.yaml`` — that file is synced between
machines and holds *settings*, while these are per-machine *runtime state*.

Named instances (tmux ``-L`` style): setting ``CLAUNCH_DAEMON=<name>`` (or
``claunch -L <name>``) selects a separate daemon *instance* whose entire
runtime state lives under ``<launcher home>/daemons/<name>/`` — its own
address file, token, singleton lock, sessions and meshes. Instances are fully
independent servers; the default (unnamed) instance keeps the classic
``daemon/`` directory, so existing setups are untouched.
"""

from __future__ import annotations

import os
import re
from pathlib import Path

from .. import config

#: Selects the daemon instance, tmux's ``-L socket-name`` analog. Empty/unset
#: means the default instance.
INSTANCE_ENV = "CLAUNCH_DAEMON"

_INSTANCE_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")


def validate_instance(name: str) -> str:
    """Return ``name`` if it is a safe instance name, else raise ValueError.

    The name becomes a directory component, so anything that could traverse
    (separators, leading dots) or confuse tooling is rejected outright.
    """
    if not _INSTANCE_RE.match(name):
        raise ValueError(
            f"bad daemon instance name {name!r} (use letters, digits, '-', '_', "
            "'.'; must start with a letter or digit, max 64 chars)"
        )
    return name


def instance() -> str:
    """The active daemon instance name ('' = the default instance)."""
    name = os.environ.get(INSTANCE_ENV, "").strip()
    return validate_instance(name) if name else ""


def daemon_dir() -> Path:
    """Root for this instance's runtime state (``~/.claude-launcher/daemon``,
    or ``~/.claude-launcher/daemons/<name>`` for a named instance)."""
    name = instance()
    if name:
        return config.launcher_home() / "daemons" / name
    return config.launcher_home() / "daemon"


def daemon_json() -> Path:
    """Address/identity file written by a running daemon (pid, host, port)."""
    return daemon_dir() / "daemon.json"


def token_file() -> Path:
    """The API auth token (created on first daemon start, ``0600``)."""
    return daemon_dir() / "token"


def lock_file() -> Path:
    """Singleton lock taken by the daemon process for its lifetime."""
    return daemon_dir() / "daemon.lock"


def log_file() -> Path:
    """The daemon's own log (also receives the detached process's stderr)."""
    return daemon_dir() / "daemon.log"


def sessions_json() -> Path:
    """Persisted session *definitions* (for listing and restore-on-restart)."""
    return daemon_dir() / "sessions.json"


def session_dir(name: str) -> Path:
    """Per-session directory holding its raw output log and metadata."""
    return daemon_dir() / "sessions" / name


def session_log(name: str) -> Path:
    """Append-only raw PTY output log for a session."""
    return session_dir(name) / "output.log"


def mesh_root() -> Path:
    """Root for mesh state (definitions, message logs, delivery cursors)."""
    return daemon_dir() / "mesh"


def mesh_dir(name: str) -> Path:
    """Per-mesh directory: ``mesh.json``, ``log.jsonl``, ``cursors.json``."""
    return mesh_root() / name
