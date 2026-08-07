"""Filesystem locations for daemon runtime state.

Everything machine-local the daemon needs (its address file, auth token, lock,
session definitions, per-session logs) lives under ``<launcher home>/daemon/``.
None of this belongs in ``~/.claunch.yaml`` — that file is synced between
machines and holds *settings*, while these are per-machine *runtime state*.
"""

from __future__ import annotations

from pathlib import Path

from .. import config


def daemon_dir() -> Path:
    """Root for all daemon runtime state (``~/.claude-launcher/daemon``)."""
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
