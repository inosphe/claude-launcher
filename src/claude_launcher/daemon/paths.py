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

#: Set by :mod:`instance_manifest` when an instance's manifest moved the
#: launcher home: the home it replaced, which is where every instance's state
#: directory (and manifest) lives.
INSTANCE_BASE_ENV = "CLAUNCH_INSTANCE_BASE"

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


def base_home() -> Path:
    """The launcher home instances are anchored in.

    Normally :func:`config.launcher_home`. When an instance manifest gave the
    instance its own home (``home:`` in ``instance.yaml``), the home that was
    in effect before it -- so named instances' state and manifests stay in one
    place however many homes the instances use.
    """
    base = os.environ.get(INSTANCE_BASE_ENV, "").strip()
    return Path(base) if base else config.launcher_home()


def known_instances() -> list:
    """Every instance with runtime state on disk: ``''`` (the default) first
    when its directory exists, then named instances sorted.

    "Known" means a state directory exists — not that a server is running;
    callers who care probe each instance's ``daemon.json``/health themselves.
    """
    names = []
    if (base_home() / "daemon").is_dir():
        names.append("")
    root = base_home() / "daemons"
    if root.is_dir():
        names.extend(sorted(
            p.name for p in root.iterdir()
            if p.is_dir() and _INSTANCE_RE.match(p.name)
        ))
    return names


def daemon_dir() -> Path:
    """Root for this instance's runtime state (``~/.claude-launcher/daemon``,
    or ``~/.claude-launcher/daemons/<name>`` for a named instance)."""
    name = instance()
    if name:
        return base_home() / "daemons" / name
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
    """Legacy JSON registry of session definitions.

    Superseded by :func:`sessions_db`; kept only so a daemon that predates the
    database can hand its records over on first start (see
    :meth:`db.SessionStore.migrate_from_json`). Nothing writes it any more.
    """
    return daemon_dir() / "sessions.json"


def sessions_db() -> Path:
    """SQLite registry of session definitions (for listing and restore).

    The durable replacement for :func:`sessions_json`: one row per session,
    each write a transaction, so a torn or empty file can no longer take the
    whole fleet with it (see :mod:`claude_launcher.daemon.db`)."""
    return daemon_dir() / "sessions.db"


def briefings_json() -> Path:
    """Persisted LLM briefing cache, retained across daemon restarts."""
    return daemon_dir() / "briefings.json"


def briefing_faq_json() -> Path:
    """User-defined briefing questions for this daemon instance."""
    return daemon_dir() / "briefing-faq.json"


def rag_dir() -> Path:
    """Vector indexes behind semantic search (daemon/rag.py): one file per
    corpus, machine-local derived data that a reindex rebuilds from the
    board and the session registry."""
    return daemon_dir() / "rag"


def prompt_presets_json() -> Path:
    """User-defined session-footer prompt presets for this daemon instance."""
    return daemon_dir() / "prompt-presets.json"


def status_checks_json() -> Path:
    """User-defined Y/N status checks and per-session agent reports."""
    return daemon_dir() / "status-checks.json"


def session_dir(name: str) -> Path:
    """Per-session directory holding its raw output log and metadata."""
    return daemon_dir() / "sessions" / name


def session_log(name: str) -> Path:
    """Append-only raw PTY output log for a session."""
    return session_dir(name) / "output.log"


def session_scratch_dir(name: str) -> Path:
    """Where a session writes its own intermediate files.

    Git Bash's ``/tmp`` is not per session on this platform: it resolves to
    the one machine-wide Temp directory, which ``TMP`` and ``TEMP`` name too.
    Two sessions that choose the same file name there overwrite each other
    with no error and no warning, and the second reader takes the first
    writer's bytes for its own. That happened -- the measurement is on
    ``claunch-shared-tmp-clobber-xjn`` -- and what is lost is not the file but
    the evidence a verdict rests on: a judgement built on overwritten values
    is wrong in a way that still reads as reasonable.

    Redirecting ``/tmp`` is not available as a fix. Running with ``TMP`` and
    ``TEMP`` pointed elsewhere still leaves ``cygpath -w /tmp`` at the
    machine-wide path, because MSYS mounts it fixed. So the prevention is a
    directory that is per session by construction: ``harness.build_command``
    exports this path as ``CLAUNCH_SCRATCH`` and ``Session.__init__`` creates
    it, which gives it the same lifetime as the session's log -- ``manager``
    removes the whole session directory when the record is cleared.
    """
    return session_dir(name) / "scratch"


def mesh_root() -> Path:
    """Root for mesh state (definitions, message logs, delivery cursors)."""
    return daemon_dir() / "mesh"


def mesh_dir(name: str) -> Path:
    """Per-mesh directory: ``mesh.json``, ``log.jsonl``, ``cursors.json``."""
    return mesh_root() / name


def reports_root() -> Path:
    """Root for session round reports — HTML a session leaves behind.

    A sibling of ``sessions/``, deliberately *not* inside it: ``clear-sessions
    --logs`` rmtree's :func:`session_dir` (see ``manager.clear``), and a report
    is the one artefact of a round that must outlive the terminal it was
    written in. The session name is only the key, not a lifetime.
    """
    return daemon_dir() / "reports"


def session_reports(name: str) -> Path:
    """Where one session's reports live: ``<daemon dir>/reports/<name>``."""
    return reports_root() / name
