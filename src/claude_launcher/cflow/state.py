"""Run state, journal, and workflow discovery for cflow.

Runs are keyed by **(working directory, scope)** and stored under
``<cwd>/.cflow/runs/<scope>/``:

- ``workflow.yaml`` — a snapshot of the workflow taken at ``start`` (mid-run
  edits to the source file cannot corrupt a running position)
- ``state.json``   — cursor, status, approvals, pending selection
- ``journal.jsonl``— append-only event log (started, delivered, completed,
  verify results, selections, approvals, done/aborted)
- ``request.json`` — a *pending start request*: a human asked (from the
  dashboard/CLI) for a workflow to be started here, for the scope's agent to
  pick up and start itself. Outlives an archive; cleared by ``start``
- ``.lock``        — held across every state transition, so the two processes
  that write a run (the agent's MCP server and the daemon) cannot interleave
- ``archive/<stamp>-<run_id>/`` — retired runs, one folder each holding the
  three files above; ``archive`` (or a new ``start`` over a finished run)
  moves them there, freeing the slot

The *scope* maps a run 1:1 to the agent session driving it: the daemon
exports ``CLAUNCH_SESSION=<name>`` into every managed session (tmux's
``$TMUX`` equivalent), the claude → MCP-server process chain inherits it,
and this module resolves it automatically — so three sessions in the same
project directory drive three independent runs. Outside a managed session
the scope falls back to ``default`` (one run per directory, the original
behaviour; a legacy flat ``.cflow/`` layout is migrated on first access).
Humans override the ambient scope explicitly (CLI ``-t``, web ``scope``).

Workflow files are looked up by name in the project first, then globally:
``<cwd>/.claunch/workflows/*.yaml`` → ``~/.claude-launcher/workflows/*.yaml``.
An explicit path (ending in .yaml/.yml) is used as-is.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import config
from . import model

#: Directory (relative to a project) holding its workflow declarations.
PROJECT_WORKFLOWS = Path(".claunch") / "workflows"

#: Environment variable the daemon sets in every managed session.
SESSION_ENV = "CLAUNCH_SESSION"

#: Scope used outside any managed session (and for pre-scope layouts).
DEFAULT_SCOPE = "default"

_scope_override: contextvars.ContextVar = contextvars.ContextVar(
    "cflow_scope", default=None
)


class StateError(Exception):
    """Raised for missing/corrupt run state."""


def current_scope() -> str:
    """The ambient scope: explicit override > session env > default."""
    return _scope_override.get() or os.environ.get(SESSION_ENV) or DEFAULT_SCOPE


def push_scope(scope: Optional[str]):
    """Set an explicit scope override; returns a token for :func:`pop_scope`."""
    return _scope_override.set(scope) if scope else None


def pop_scope(token) -> None:
    if token is not None:
        _scope_override.reset(token)


def global_workflows_dir() -> Path:
    return config.launcher_home() / "workflows"


def runs_registry_path() -> Path:
    return config.launcher_home() / "cflow_runs.json"


def register_run_dir(cwd: Optional[str] = None, scope: Optional[str] = None) -> None:
    """Record this run's (directory, scope) in the machine-local registry.

    The daemon web dashboard scans the registry, so runs are monitorable no
    matter where they were started (a managed session, a plain terminal, an
    orchestrator script). Best-effort: registry loss only affects listing.
    """
    target = {
        "cwd": str(Path(cwd or os.getcwd()).resolve()),
        "scope": scope or current_scope(),
    }
    entries = [e for e in _read_registry() if e != target]
    entries.append(target)
    _write_registry(entries)


def _run_alive(cwd: str, scope: str) -> bool:
    base = Path(cwd) / ".cflow"
    slot = base / "runs" / scope
    # A pending start request keeps the slot listed even with no run yet —
    # that is precisely the state a human wants to watch after asking for one.
    if (slot / "state.json").is_file() or (slot / REQUEST_FILE).is_file():
        return True
    # legacy flat layout counts as the default scope until migrated
    return scope == DEFAULT_SCOPE and (base / "state.json").is_file()


def known_runs() -> List[Tuple[str, str]]:
    """Registered ``(cwd, scope)`` pairs that still hold run state
    (pruned on read)."""
    entries = _read_registry()
    alive = [e for e in entries if _run_alive(e["cwd"], e["scope"])]
    if alive != entries:
        _write_registry(alive)
    return [(e["cwd"], e["scope"]) for e in alive]


def _read_registry() -> List[Dict[str, str]]:
    try:
        entries = json.loads(runs_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    if not isinstance(entries, list):
        return []
    out: List[Dict[str, str]] = []
    for e in entries:
        if isinstance(e, str):  # pre-scope registry format
            out.append({"cwd": e, "scope": DEFAULT_SCOPE})
        elif isinstance(e, dict) and e.get("cwd"):
            out.append({"cwd": str(e["cwd"]), "scope": str(e.get("scope") or DEFAULT_SCOPE)})
    return out


def _write_registry(entries: List[Dict[str, str]]) -> None:
    path = runs_registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except OSError:
        pass


def cflow_dir(cwd: Optional[str] = None) -> Path:
    return Path(cwd or os.getcwd()) / ".cflow"


def scope_dir(cwd: Optional[str] = None, scope: Optional[str] = None) -> Path:
    scope = scope or current_scope()
    target = cflow_dir(cwd) / "runs" / scope
    if scope == DEFAULT_SCOPE:
        _migrate_legacy(cflow_dir(cwd), target)
    return target


def _migrate_legacy(base: Path, target: Path) -> None:
    """Move a pre-scope flat ``.cflow/`` layout into ``runs/default/``."""
    if not (base / "state.json").is_file() or (target / "state.json").is_file():
        return
    try:
        target.mkdir(parents=True, exist_ok=True)
        for name in ("state.json", "workflow.yaml", "journal.jsonl"):
            src = base / name
            if src.is_file():
                src.rename(target / name)
    except OSError:
        pass


def scopes_in(cwd: Optional[str] = None) -> List[str]:
    """Scopes with run state (or a pending start request) in this directory
    (legacy layout = default)."""
    base = cflow_dir(cwd)
    out: List[str] = []
    if (base / "state.json").is_file():
        out.append(DEFAULT_SCOPE)
    runs = base / "runs"
    if runs.is_dir():
        for entry in sorted(runs.iterdir()):
            has = (entry / "state.json").is_file() or (entry / REQUEST_FILE).is_file()
            if has and entry.name not in out:
                out.append(entry.name)
    return out


def _state_path(cwd: Optional[str], scope: Optional[str] = None) -> Path:
    return scope_dir(cwd, scope) / "state.json"


def _snapshot_path(cwd: Optional[str], scope: Optional[str] = None) -> Path:
    return scope_dir(cwd, scope) / "workflow.yaml"


def journal_path(cwd: Optional[str] = None, scope: Optional[str] = None) -> Path:
    return scope_dir(cwd, scope) / "journal.jsonl"


# --------------------------------------------------------------------------- #
# cross-process lock
# --------------------------------------------------------------------------- #
#: Name of the per-scope lock file.
LOCK_FILE = ".lock"

#: How long to wait for another process to release the slot before failing.
#: Every holder does a handful of small file writes, so this is a very long
#: time in practice — verify commands deliberately run *outside* the lock.
LOCK_TIMEOUT = 10.0

#: A lock file older than this is treated as abandoned (its holder crashed or
#: was killed mid-transition) and reclaimed.
LOCK_STALE_AFTER = 120.0

_POLL = 0.02


class LockBusy(StateError):
    """The scope's lock could not be taken (another process holds it)."""


def _lock_stale(path: Path) -> bool:
    try:
        return (time.time() - path.stat().st_mtime) > LOCK_STALE_AFTER
    except OSError:
        return False


@contextlib.contextmanager
def run_lock(
    cwd: Optional[str] = None,
    scope: Optional[str] = None,
    *,
    timeout: float = LOCK_TIMEOUT,
):
    """Hold the ``(cwd, scope)`` slot exclusively for a state transition.

    Two *processes* write a run — the daemon (web/CLI actions) and the agent's
    own MCP server (``start``/``report``/``next``/``select``) — and the run is
    several files (``state.json`` + ``workflow.yaml`` + the journal). Without
    this, two concurrent starts both see an empty slot and both write: the
    survivor can end up with one workflow's snapshot and another's cursor.

    An exclusively-created lock file is the portable primitive here (Windows
    has no ``flock``); a lock left behind by a killed process is reclaimed
    after :data:`LOCK_STALE_AFTER`.
    """
    path = scope_dir(cwd, scope) / LOCK_FILE
    path.parent.mkdir(parents=True, exist_ok=True)
    deadline = time.monotonic() + timeout
    fd = None
    while True:
        try:
            fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            if _lock_stale(path):
                _unlink(path)
                continue
            if time.monotonic() >= deadline:
                raise LockBusy(
                    f"another process is changing this cflow run "
                    f"({path.parent}); try again in a moment"
                )
            time.sleep(_POLL)
        except OSError as exc:
            raise StateError(f"cannot lock cflow run at {path}: {exc}") from exc
    try:
        try:
            os.write(fd, f"{os.getpid()} {utcnow()}".encode("utf-8"))
        except OSError:
            pass
        os.close(fd)
        fd = None
        yield
    finally:
        if fd is not None:
            os.close(fd)
        _unlink(path)


def _unlink(path: Path) -> None:
    try:
        path.unlink()
    except OSError:
        pass


# --------------------------------------------------------------------------- #
# pending start request
# --------------------------------------------------------------------------- #
#: Name of the pending start-request file inside a scope directory.
REQUEST_FILE = "request.json"


def _request_path(cwd: Optional[str] = None, scope: Optional[str] = None) -> Path:
    return scope_dir(cwd, scope) / REQUEST_FILE


def read_request(
    cwd: Optional[str] = None, scope: Optional[str] = None
) -> Optional[dict]:
    """The pending start request for this slot, if a human filed one."""
    try:
        doc = json.loads(_request_path(cwd, scope).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return doc if isinstance(doc, dict) and doc.get("workflow") else None


def write_request(request: dict, cwd: Optional[str] = None) -> None:
    path = _request_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(request, ensure_ascii=False, indent=2), encoding="utf-8")


def clear_request(cwd: Optional[str] = None) -> None:
    _unlink(_request_path(cwd))


# --------------------------------------------------------------------------- #
# workflow discovery
# --------------------------------------------------------------------------- #
def search_dirs(cwd: Optional[str] = None) -> List[Path]:
    return [Path(cwd or os.getcwd()) / PROJECT_WORKFLOWS, global_workflows_dir()]


def list_workflows(cwd: Optional[str] = None) -> List[Tuple[str, Path]]:
    """All available ``(name, path)`` pairs, project first, deduped by name."""
    out: List[Tuple[str, Path]] = []
    seen = set()
    for base in search_dirs(cwd):
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.y*ml")):
            if path.stem not in seen:
                seen.add(path.stem)
                out.append((path.stem, path))
    return out


def find_workflow(ref: str, cwd: Optional[str] = None) -> Path:
    """Resolve a workflow reference: an explicit path, or a name to search."""
    if ref.endswith((".yaml", ".yml")):
        path = Path(ref).expanduser()
        if not path.is_absolute():
            path = Path(cwd or os.getcwd()) / path
        if path.is_file():
            return path
        raise model.WorkflowError(f"workflow file not found: {path}")
    for base in search_dirs(cwd):
        for suffix in (".yaml", ".yml"):
            path = base / f"{ref}{suffix}"
            if path.is_file():
                return path
    names = ", ".join(n for n, _ in list_workflows(cwd)) or "(none)"
    raise model.WorkflowError(
        f"no workflow named {ref!r} (available: {names}; "
        f"searched {', '.join(str(d) for d in search_dirs(cwd))})"
    )


# --------------------------------------------------------------------------- #
# run state
# --------------------------------------------------------------------------- #
def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def has_run(cwd: Optional[str] = None) -> bool:
    return _state_path(cwd).is_file()


def load_state(cwd: Optional[str] = None) -> dict:
    path = _state_path(cwd)
    if not path.is_file():
        raise StateError(
            "no active cflow run in this directory (start one with the "
            "cflow 'start' tool or see 'claunch cflow ls')"
        )
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(f"corrupt cflow state at {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise StateError(f"corrupt cflow state at {path}")
    return doc


def save_state(state: dict, cwd: Optional[str] = None) -> None:
    path = _state_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")


def clear_state(cwd: Optional[str] = None) -> None:
    for path in (_state_path(cwd), _snapshot_path(cwd)):
        try:
            path.unlink()
        except OSError:
            pass


#: Files that make up one run inside its scope directory.
RUN_FILES = ("state.json", "workflow.yaml", "journal.jsonl")


def archive_run(cwd: Optional[str] = None) -> Path:
    """Retire the scope's current run: move its files (journal included)
    into ``archive/<stamp>-<run_id>/`` inside the scope directory, freeing
    the (cwd, scope) slot for a new ``start``. Returns the archive folder."""
    state = load_state(cwd)  # StateError if there is nothing to archive
    sdir = scope_dir(cwd)
    run_id = str(state.get("run_id") or "run")
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    target = sdir / "archive" / f"{stamp}-{run_id}"
    n = 0
    while target.exists():
        n += 1
        target = sdir / "archive" / f"{stamp}-{run_id}-{n}"
    target.mkdir(parents=True)
    for name in RUN_FILES:
        src = sdir / name
        if src.is_file():
            src.rename(target / name)
    return target


def snapshot_workflow(text: str, cwd: Optional[str] = None) -> None:
    path = _snapshot_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_snapshot(cwd: Optional[str] = None, scope: Optional[str] = None) -> model.Workflow:
    path = _snapshot_path(cwd, scope)
    if not path.is_file():
        raise StateError("cflow run state exists but the workflow snapshot is missing")
    return model.load(path)


def journal(event: str, data: Optional[Dict] = None, cwd: Optional[str] = None) -> None:
    entry = {"at": utcnow(), "event": event}
    if data:
        entry.update(data)
    path = journal_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")


def read_journal(
    cwd: Optional[str] = None,
    scope: Optional[str] = None,
    *,
    run_id: Optional[str] = None,
) -> List[dict]:
    path = journal_path(cwd, scope)
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        if run_id is None or entry.get("run") == run_id:
            out.append(entry)
    return out
