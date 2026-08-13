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
Humans override the ambient scope explicitly (CLI ``-t``, web ``scope``) — and
because that override becomes a directory name, it is checked against the
session-name spelling on the way in (:func:`normalize_scope`).

Workflow files are looked up by name in the project first, then globally:
``<cwd>/.claunch/workflows/*.yaml`` → ``~/.claude-launcher/workflows/*.yaml``.
An explicit path (ending in .yaml/.yml) is used as-is. The global layer is
where the workflows shipped with claunch land — ``claunch install`` copies
them out of the package (:func:`bundled_workflows_dir`), and ``claunch cflow
add`` puts more there — so a project directory only needs a file of its own
when it wants to *differ*. A name that exists in both layers is not
ambiguous, but the loser is reported (:class:`Located`) rather than silently
dropped, because two copies of one workflow otherwise drift unnoticed.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import config
from . import model

#: Directory (relative to a project) holding its workflow declarations.
PROJECT_WORKFLOWS = Path(".claunch") / "workflows"

#: The layers a name is searched in, nearest first. A project's own
#: declarations override the shared ones; nothing overrides a project.
LAYER_PROJECT = "project"
LAYER_GLOBAL = "global"

#: Origin reported for a reference that named a file outright, which belongs
#: to no layer at all.
LAYER_FILE = "file"

#: Environment variable the daemon sets in every managed session.
SESSION_ENV = "CLAUNCH_SESSION"

#: Scope used outside any managed session (and for pre-scope layouts).
DEFAULT_SCOPE = "default"

#: A scope names one managed session, so it is spelled like one (the daemon's
#: own session-name rule) — and it becomes a directory under ``.cflow/runs/``.
_SCOPE_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Legal names that are not legal *path components*: ``runs/..`` is ``.cflow``
#: itself, where the legacy flat layout lives, and ``runs/.`` is the runs
#: directory. Neither is a slot.
_SCOPE_RESERVED = frozenset({".", ".."})

_scope_override: contextvars.ContextVar = contextvars.ContextVar(
    "cflow_scope", default=None
)


class StateError(Exception):
    """Raised for missing/corrupt run state."""


def valid_scope(scope: Optional[str]) -> bool:
    """Whether ``scope`` can name a run slot."""
    return bool(
        scope and _SCOPE_RE.match(scope) and scope not in _SCOPE_RESERVED
    )


def normalize_scope(scope: Optional[str]) -> str:
    """The scope as a path component, or :class:`StateError`.

    A scope reaches this module from three places: the session env (set by the
    daemon, trustworthy), a CLI flag, and the web dashboard's query string or
    JSON body — which is reachable from off the machine whenever the daemon is
    relayed. Since the value goes on to *be* a directory name, an unchecked
    one ('../../..') would read and write run files anywhere on disk. The
    check therefore lives at the single point where a scope turns into a path,
    not at each caller.
    """
    scope = (scope or "").strip()
    if not valid_scope(scope):
        raise StateError(
            f"invalid cflow scope {scope!r}: a scope is a session name — "
            f"letters, digits, '.', '_' or '-'"
        )
    return scope


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


def bundled_workflows_dir() -> Path:
    """The workflows that ship inside the package.

    Not a search layer: nothing resolves a name here. ``claunch install``
    copies these out into :func:`global_workflows_dir`, where they become
    ordinary files a human may edit, override per project, or delete. Keeping
    them in the package (rather than in this checkout's ``.claunch/``) is what
    makes them survive a wheel install, which has no checkout to read.
    """
    return Path(__file__).resolve().parent.parent / "workflows"


def bundled_workflows() -> List[Tuple[str, Path]]:
    """The ``(name, path)`` pairs shipped with claunch."""
    base = bundled_workflows_dir()
    if not base.is_dir():
        return []
    return [(p.stem, p) for p in sorted(base.glob("*.y*ml"))]


def runs_registry_path() -> Path:
    return config.launcher_home() / "cflow_runs.json"


def register_run_dir(cwd: Optional[str] = None, scope: Optional[str] = None) -> None:
    """Record this run's (directory, scope) in the machine-local registry.

    The daemon web dashboard scans the registry, so runs are monitorable no
    matter where they were started (a managed session, a plain terminal, an
    orchestrator script). Best-effort: registry loss only affects listing.
    """
    target = {
        "cwd": resolve_cwd(cwd),
        "scope": normalize_scope(scope or current_scope()),
    }
    entries = [e for e in _read_registry() if e != target]
    entries.append(target)
    _write_registry(entries)


def _run_alive(cwd: str, scope: str) -> bool:
    if not valid_scope(scope):
        return False  # nothing legitimate wrote it; drop it on the next read
    base = cflow_dir(cwd)
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


def resolve_cwd(cwd: Optional[str] = None) -> str:
    """A run's directory, canonical.

    Both processes that write a run have to agree byte-for-byte on which slot
    they are in, and they arrive at it differently: the agent's MCP server
    passes whatever ``os.getcwd()`` it inherited, while every daemon entry
    point passes its own resolved copy. Half of what identifies a run is this
    string — the registry stores it, ``_scope_sessions`` compares it — so it
    is canonicalised in one place rather than at each caller.
    """
    return str(Path(cwd or os.getcwd()).resolve())


def cflow_dir(cwd: Optional[str] = None) -> Path:
    return Path(resolve_cwd(cwd)) / ".cflow"


def scope_dir(cwd: Optional[str] = None, scope: Optional[str] = None) -> Path:
    scope = normalize_scope(scope or current_scope())
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
            if not valid_scope(entry.name):
                continue
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
@dataclass(frozen=True)
class Located:
    """A resolved workflow, and what resolving it passed over.

    ``shadows`` is the point: a name that exists in more than one layer is not
    ambiguous — the nearest layer wins — but the losers are worth naming,
    because two copies of one workflow drift silently otherwise. Every surface
    that lists workflows carries this so a human can see which file will run.
    """

    name: str
    path: Path
    origin: str
    shadows: Tuple[Path, ...] = ()

    @property
    def overrides(self) -> bool:
        return bool(self.shadows)


def search_layers(cwd: Optional[str] = None) -> List[Tuple[str, Path]]:
    """The layers a name is searched in, nearest first."""
    return [
        (LAYER_PROJECT, Path(cwd or os.getcwd()) / PROJECT_WORKFLOWS),
        (LAYER_GLOBAL, global_workflows_dir()),
    ]


def search_dirs(cwd: Optional[str] = None) -> List[Path]:
    return [base for _, base in search_layers(cwd)]


def _candidates(cwd: Optional[str] = None) -> Dict[str, List[Tuple[str, Path]]]:
    """Every declaration of every name, per name, nearest layer first."""
    found: Dict[str, List[Tuple[str, Path]]] = {}
    for layer, base in search_layers(cwd):
        if not base.is_dir():
            continue
        for path in sorted(base.glob("*.y*ml")):
            found.setdefault(path.stem, []).append((layer, path))
    return found


def resolved_workflows(cwd: Optional[str] = None) -> List[Located]:
    """Every available workflow, each one knowing what it shadows."""
    out = []
    for name, hits in _candidates(cwd).items():
        layer, path = hits[0]
        out.append(Located(name, path, layer, tuple(p for _, p in hits[1:])))
    return sorted(out, key=lambda w: w.name)


def list_workflows(cwd: Optional[str] = None) -> List[Tuple[str, Path]]:
    """All available ``(name, path)`` pairs, project first, deduped by name."""
    return [(w.name, w.path) for w in resolved_workflows(cwd)]


def locate(ref: str, cwd: Optional[str] = None) -> Located:
    """Resolve a workflow reference: an explicit path, or a name to search."""
    if ref.endswith((".yaml", ".yml")):
        path = Path(ref).expanduser()
        if not path.is_absolute():
            path = Path(cwd or os.getcwd()) / path
        if path.is_file():
            return Located(path.stem, path, LAYER_FILE)
        raise model.WorkflowError(f"workflow file not found: {path}")
    hits = _candidates(cwd).get(ref)
    if hits:
        layer, path = hits[0]
        return Located(ref, path, layer, tuple(p for _, p in hits[1:]))
    names = ", ".join(n for n, _ in list_workflows(cwd)) or "(none)"
    raise model.WorkflowError(
        f"no workflow named {ref!r} (available: {names}; "
        f"searched {', '.join(str(d) for d in search_dirs(cwd))})"
    )


def find_workflow(ref: str, cwd: Optional[str] = None) -> Path:
    """Where a reference resolves to, for callers that need only the file."""
    return locate(ref, cwd).path


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
