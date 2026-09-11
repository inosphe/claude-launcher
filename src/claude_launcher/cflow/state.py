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

A project file does not have to be a whole second copy. Declaring ``extends:
<name>`` makes it a *layer* over the workflow that name resolves to below it:
the base is read first and this file's properties are merged onto it one at a
time (:func:`model.merge_docs`), so a repository that only needs its own
``verify`` commands writes those and inherits everything else. Bases are
resolved here rather than in the parser — which layers exist is a fact about
the directory, not about the file (:func:`resolve_base`) — and a run
snapshots the composed result, never the overlay alone.
"""

from __future__ import annotations

import contextlib
import copy
import contextvars
import json
import os
import re
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Sequence, Tuple

from .. import config
from .. import journal as journal_mod
from . import model

#: Directory (relative to a project) holding its workflow declarations.
PROJECT_WORKFLOWS = Path(".claunch") / "workflows"

#: The layers a name is searched in, nearest first. A project's own
#: declarations override the global ones; nothing overrides a project.
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


# --------------------------------------------------------------------------- #
# sub runs: N slots under one scope
# --------------------------------------------------------------------------- #
#: Directory under a scope that holds its SUB runs: ``runs/<scope>/sub/<name>/``,
#: each a full slot (the same :data:`RUN_FILES`, lock, journal and archive as
#: the scope's main run). A sub run is one more state machine the SAME session
#: drives beside its main run — a side track with its own steps and gates — so
#: it lives *inside* the scope rather than beside it: the daemon's rule that a
#: scope names exactly one driving session stays true, and ending the main run
#: can cascade to what it owned.
SUB_DIR = "sub"

#: The main run's name in every place a run is addressed. Not a directory —
#: the main run keeps living directly under ``runs/<scope>/``.
MAIN_RUN = "main"

#: A sub run name is spelled like a scope (it becomes a directory too).
_RUN_RE = _SCOPE_RE

_run_override: contextvars.ContextVar = contextvars.ContextVar(
    "cflow_run", default=None
)


def valid_run_name(run: Optional[str]) -> bool:
    """Whether ``run`` can name a sub run slot (``main`` is reserved)."""
    return bool(
        run and _RUN_RE.match(run) and run not in _SCOPE_RESERVED and run != MAIN_RUN
    )


def normalize_run(run: Optional[str]) -> Optional[str]:
    """``None`` for the main run, else the sub run name as a path component.

    Like :func:`normalize_scope` this is the one point where a name from a
    tool argument, a CLI flag or a request body turns into a directory, so it
    is the one point that refuses ``..`` and friends.
    """
    run = (run or "").strip()
    if not run or run == MAIN_RUN:
        return None
    if not valid_run_name(run):
        raise StateError(
            f"invalid cflow sub run name {run!r}: letters, digits, '.', '_' "
            f"or '-' ({MAIN_RUN!r} is the main run)"
        )
    return run


def current_run() -> Optional[str]:
    """The ambient sub run name, or ``None`` for the scope's main run.

    Unlike the scope there is no environment half: a session's environment
    names the session, never which of its runs a call is about. Only an
    explicit override (a tool's ``run`` argument, the CLI's ``--run``) selects
    a sub run; every unqualified call is about the main run, exactly as it
    was before sub runs existed.
    """
    return _run_override.get() or None


def push_run(run: Optional[str]):
    """Set an explicit run override; returns a token for :func:`pop_run`.

    ``None`` leaves the ambient run alone; :data:`MAIN_RUN` (or ``""``)
    forces the main run even inside a sub run's call — that is how a sub
    run's start writes its ``sub_started`` event into the main journal.
    """
    if run is None:
        return None
    return _run_override.set(normalize_run(run) or "")


def pop_run(token) -> None:
    if token is not None:
        _run_override.reset(token)


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


def load_bundled(ref) -> model.Workflow:
    """One shipped workflow, composed against its shipped siblings.

    The bundle is not a search layer (see :func:`bundled_workflows_dir`), so
    :func:`load_workflow` cannot answer for it — and :func:`model.load`
    refuses a file that ``extends`` another. A shipped *layer*
    (``improv-worker-remote`` over ``improv-worker``) still has to be read as
    the workflow it composes to, by the tests that hold every shipped file to
    the same rules and by anyone asking what the package teaches. The base of
    a shipped layer is the shipped file of that name, next to it — nothing
    else is meaningful at install time, when no global layer exists yet.

    ``ref`` is a name or a path inside the bundle.
    """
    path = Path(ref) if isinstance(ref, Path) else bundled_workflows_dir() / f"{ref}.yaml"

    def _sibling(base: str, from_path: Path) -> Path:
        if base.endswith((".yaml", ".yml")):
            candidate = from_path.parent / base
        else:
            candidate = from_path.parent / f"{base}.yaml"
        if not candidate.is_file():
            raise model.WorkflowError(
                f"{from_path} extends {base!r}, but the bundle ships no such "
                f"workflow next to it ({candidate})"
            )
        return candidate

    return model.compose(path, resolve=_sibling).workflow


def bundled_workflow_assets() -> List[Path]:
    """Non-workflow files that ship alongside the bundled workflows.

    Verify scripts (``*-verify.mjs``) that a workflow's ``verify:`` command
    resolves at run time — project copy first, then the global layer. No
    name resolves to these; they only have to land in the same directory as
    the yaml they serve, so seeding copies them and nothing else reads them.
    """
    base = bundled_workflows_dir()
    if not base.is_dir():
        return []
    return sorted(base.glob("*.mjs"))


def runs_registry_path() -> Path:
    return config.launcher_home() / "cflow_runs.json"


def register_run_dir(
    cwd: Optional[str] = None, scope: Optional[str] = None, run: Optional[str] = None
) -> None:
    """Record this run's (directory, scope[, sub run]) in the machine-local registry.

    The daemon web dashboard scans the registry, so runs are monitorable no
    matter where they were started (a managed session, a plain terminal, an
    orchestrator script). Best-effort: registry loss only affects listing.
    A sub run is recorded with its name; the main run's entry has no ``run``
    key, which is also the shape every pre-sub-run registry file has.
    """
    target = {
        "cwd": resolve_cwd(cwd),
        "scope": normalize_scope(scope or current_scope()),
    }
    sub = normalize_run(run) if run is not None else current_run()
    if sub:
        target["run"] = sub
    entries = [e for e in _read_registry() if e != target]
    entries.append(target)
    _write_registry(entries)


def _run_alive(cwd: str, scope: str, run: str = "") -> bool:
    if not valid_scope(scope):
        return False  # nothing legitimate wrote it; drop it on the next read
    base = cflow_dir(cwd)
    slot = base / "runs" / scope
    if run:
        if not valid_run_name(run):
            return False
        return (slot / SUB_DIR / run / "state.json").is_file()
    # A pending start request keeps the slot listed even with no run yet —
    # that is precisely the state a human wants to watch after asking for one.
    if (slot / "state.json").is_file() or (slot / REQUEST_FILE).is_file():
        return True
    # legacy flat layout counts as the default scope until migrated
    return scope == DEFAULT_SCOPE and (base / "state.json").is_file()


def _alive_entries() -> List[Dict[str, str]]:
    entries = _read_registry()
    alive = [e for e in entries if _run_alive(e["cwd"], e["scope"], e.get("run", ""))]
    if alive != entries:
        _write_registry(alive)
    return alive


def known_runs() -> List[Tuple[str, str]]:
    """Registered ``(cwd, scope)`` pairs whose MAIN slot still holds run state
    (pruned on read). Sub runs are listed by :func:`known_sub_runs`; keeping
    them out of here is what lets every reader keyed on ``(cwd, scope)`` —
    the daemon's clocks, the runs list — go on reading one run per scope."""
    return [(e["cwd"], e["scope"]) for e in _alive_entries() if not e.get("run")]


def known_sub_runs() -> List[Tuple[str, str, str]]:
    """Registered ``(cwd, scope, run)`` triples of sub runs still holding
    run state (pruned on read)."""
    return [(e["cwd"], e["scope"], e["run"]) for e in _alive_entries() if e.get("run")]


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
            entry = {"cwd": str(e["cwd"]), "scope": str(e.get("scope") or DEFAULT_SCOPE)}
            if e.get("run"):
                entry["run"] = str(e["run"])
            out.append(entry)
    return out


def _write_registry(entries: List[Dict[str, str]]) -> None:
    path = runs_registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except OSError:
        pass


#: How long a canonicalised directory is trusted (see :func:`resolve_cwd`).
RESOLVE_TTL = 30.0

#: Canonical form of a directory, keyed by the string handed in.
_resolved: Dict[str, Tuple[str, float]] = {}


def resolve_cwd(cwd: Optional[str] = None) -> str:
    """A run's directory, canonical.

    Both processes that write a run have to agree byte-for-byte on which slot
    they are in, and they arrive at it differently: the agent's MCP server
    passes whatever ``os.getcwd()`` it inherited, while every daemon entry
    point passes its own resolved copy. Half of what identifies a run is this
    string — the registry stores it, ``_scope_sessions`` compares it — so it
    is canonicalised in one place rather than at each caller.

    Remembered for :data:`RESOLVE_TTL`, because the daemon asks the same
    question hundreds of times a second: the runs list walks every known slot
    on a two-second poll, and every state/snapshot/journal/request path under
    a slot resolves its directory again. Each of those is a realpath walk down
    the whole path, which on Windows is a syscall per component. What the
    cache can be wrong about is a directory that becomes a symlink (or moves)
    while the daemon runs -- a reconfiguration, not a thing that happens
    mid-poll -- and it is wrong for at most the TTL.
    """
    raw = cwd or os.getcwd()
    now = time.monotonic()
    hit = _resolved.get(raw)
    if hit is not None and now - hit[1] < RESOLVE_TTL:
        return hit[0]
    out = str(Path(raw).resolve())
    _resolved[raw] = (out, now)
    return out


def cflow_dir(cwd: Optional[str] = None) -> Path:
    return Path(resolve_cwd(cwd)) / ".cflow"


#: Where a session may WRITE a workflow for another session to run, relative
#: to a project directory: ``<cwd>/.cflow/generated/``. A PM plans a child's
#: procedure as a small overlay here (``extends: improv-worker`` plus the
#: steps, checks and waits this one child needs) and names the file in the
#: spawn. It lives under ``.cflow/`` on purpose: that directory is already
#: machine-local run state (gitignored), so a generated procedure is never
#: mistaken for a declared one — the project and global layers stay the only
#: places a workflow is *declared*, and ``cflow ls`` does not list these.
GENERATED_WORKFLOWS = Path(".cflow") / "generated"


def generated_workflows_dir(cwd: Optional[str] = None) -> Path:
    return cflow_dir(cwd) / "generated"


def generated_workflow(ref: str, *, cwd: Optional[str], roots: Sequence[Optional[str]] = ()) -> Path:
    """The generated workflow file ``ref`` names, or a :class:`WorkflowError`.

    ``ref`` is a ``.yaml``/``.yml`` path — absolute, or relative to ``cwd``
    (the directory the run will stand in). It is admitted only from the
    generated directory of ``cwd`` or of one of ``roots`` (the spawning
    session's directory, so a parent in the main checkout can hand a child
    in its own worktree a file the parent wrote). Anything else — a declared
    layer, a file elsewhere in the tree, a path that climbs out with ``..`` —
    is refused: a spawn names a workflow by NAME for those, and a path that
    could point anywhere would let a request run any file on the machine as
    a procedure. The file is not parsed here; the caller loads it so a broken
    overlay is refused before a session exists for it.
    """
    if not ref.endswith((".yaml", ".yml")):
        raise model.WorkflowError(f"{ref!r} is not a workflow file path")
    path = Path(ref).expanduser()
    if not path.is_absolute():
        path = Path(resolve_cwd(cwd)) / path
    path = _same(path)
    allowed = []
    for base in (cwd, *roots):
        if not base:
            continue
        home = _same(generated_workflows_dir(base))
        if home not in allowed:
            allowed.append(home)
    if not any(path.parent == home for home in allowed):
        raise model.WorkflowError(
            f"{ref!r} is outside the generated workflow directory — a spawn may "
            f"name a workflow file only from {', '.join(str(a) for a in allowed) or GENERATED_WORKFLOWS}"
        )
    if not path.is_file():
        raise model.WorkflowError(f"generated workflow file not found: {path}")
    return path


def scope_dir(
    cwd: Optional[str] = None, scope: Optional[str] = None, run: Optional[str] = None
) -> Path:
    """The slot directory: ``runs/<scope>/`` for the main run, or
    ``runs/<scope>/sub/<run>/`` for a sub run (``run`` explicit, else the
    ambient one from :func:`push_run`). Every run file path in this module
    goes through here, so a sub run is a full slot by construction."""
    scope = normalize_scope(scope or current_scope())
    target = cflow_dir(cwd) / "runs" / scope
    if scope == DEFAULT_SCOPE:
        _migrate_legacy(cflow_dir(cwd), target)
    sub = normalize_run(run) if run is not None else current_run()
    if sub:
        target = target / SUB_DIR / sub
    return target


def sub_runs(cwd: Optional[str] = None, scope: Optional[str] = None) -> List[str]:
    """Names of this scope's sub runs that hold run state, sorted."""
    base = scope_dir(cwd, scope, run=MAIN_RUN) / SUB_DIR
    if not base.is_dir():
        return []
    return sorted(
        entry.name
        for entry in base.iterdir()
        if valid_run_name(entry.name) and (entry / "state.json").is_file()
    )


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
# cadence record: when a paced select option was last taken
# --------------------------------------------------------------------------- #
#: Per-scope record of the last take of every paced option, keyed
#: ``<workflow>:<step>:<option>`` -> ISO timestamp. Deliberately NOT one of
#: :data:`RUN_FILES`: a cadence is a fact about the slot, not about one run —
#: a recurring workflow's round N+1 must pace against round N's take, and
#: archiving the finished round must not reset the clock.
WINDOWS_FILE = "windows.json"


def _windows_path(cwd: Optional[str] = None, scope: Optional[str] = None) -> Path:
    return scope_dir(cwd, scope) / WINDOWS_FILE


def window_key(workflow: str, step_id: str, option: str) -> str:
    return f"{workflow}:{step_id}:{option}"


def read_windows(cwd: Optional[str] = None, scope: Optional[str] = None) -> Dict[str, str]:
    """Every paced option's last take in this slot (empty when none)."""
    try:
        doc = json.loads(_windows_path(cwd, scope).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(doc, dict):
        return {}
    return {str(k): str(v) for k, v in doc.items() if isinstance(v, str)}


def record_window(key: str, at: str, cwd: Optional[str] = None) -> None:
    """Note that the paced option ``key`` was taken at ``at`` (ISO)."""
    path = _windows_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    doc = read_windows(cwd)
    doc[key] = at
    path.write_text(json.dumps(doc, ensure_ascii=False, indent=2), encoding="utf-8")


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
# layering: a file that declares `extends:` is merged over its base
# --------------------------------------------------------------------------- #
def layer_of(path: Path, cwd: Optional[str] = None) -> str:
    """Which layer a file sits in, or :data:`LAYER_FILE` for one that sits in
    neither. Compared by directory rather than by name: two layers hold files
    of the same name on purpose, which is the whole point of layering."""
    parent = _same(path.parent)
    for layer, base in search_layers(cwd):
        if _same(base) == parent:
            return layer
    return LAYER_FILE


def _same(path: Path) -> Path:
    try:
        return path.resolve()
    except OSError:
        return path.absolute()


def resolve_base(ref: str, from_path: Path, cwd: Optional[str] = None) -> Path:
    """The file ``from_path`` means by ``extends: <ref>``.

    A ref ending in .yaml/.yml is a path, read against the extending file's own
    directory — the spelling for a base that is not a published workflow but a
    piece of one repository's own arrangement.

    Anything else is a workflow NAME, searched nearest-first from the
    extending file's OWN layer downward, skipping the extending file itself.
    Two consequences, and both are the point:

    * a project's ``improv-worker.yaml`` may say ``extends: improv-worker``
      and mean the global copy — the name it shadows is the name it layers
      over, and having to spell that as a path would tie the project file to
      wherever the global layer happens to live on this machine;
    * a file never reaches *upward*. A global workflow cannot pick up a
      project's file of the same name, so a base is the same file for every
      project that runs it, and one project cannot quietly redefine what
      another one's runs are built on.
    """
    if ref.endswith((".yaml", ".yml")):
        path = Path(ref).expanduser()
        if not path.is_absolute():
            path = from_path.parent / path
        if path.is_file():
            # Normalised, because this path is written into the run's journal
            # and its state: "../../elsewhere/base.yaml" answers "which file
            # is this run built on" only for a reader standing where the
            # overlay stands.
            return _same(path)
        raise model.WorkflowError(
            f"{from_path} extends {ref!r}, which is not a file ({path})"
        )
    layers = search_layers(cwd)
    start = 0
    mine = layer_of(from_path, cwd)
    for i, (layer, _base) in enumerate(layers):
        if layer == mine:
            start = i
            break
    me = _same(from_path)
    searched = []
    for _layer, base in layers[start:]:
        searched.append(base)
        if not base.is_dir():
            continue
        for candidate in sorted(base.glob(f"{ref}.y*ml")):
            if _same(candidate) != me:
                return candidate
    raise model.WorkflowError(
        f"{from_path} extends {ref!r}, but no workflow of that name was found "
        f"below it (searched {', '.join(str(d) for d in searched) or '(nothing)'})"
        + (
            ""
            if start == 0
            else " — a base is searched from the extending file's own layer "
            "downward, never upward"
        )
    )


def base_resolver(cwd: Optional[str] = None):
    """The ``extends`` resolver :func:`model.compose` needs, bound to a cwd."""

    def _resolve(ref: str, from_path: Path) -> Path:
        return resolve_base(ref, from_path, cwd)

    return _resolve


def compose_located(located: Located, cwd: Optional[str] = None) -> model.Composed:
    """Load an already-resolved workflow, following any ``extends`` chain."""
    return model.compose(located.path, resolve=base_resolver(cwd))


def load_workflow(ref: str, cwd: Optional[str] = None) -> model.Composed:
    """Resolve a workflow reference and load it, layers and all.

    The one entry point every caller that *runs* or *reads* a workflow should
    use: it is where a name becomes a file (:func:`locate`) and where a file
    becomes the merge of itself and its bases (:func:`model.compose`).
    """
    return compose_located(locate(ref, cwd), cwd)


# --------------------------------------------------------------------------- #
# run state
# --------------------------------------------------------------------------- #
def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def has_run(cwd: Optional[str] = None) -> bool:
    return _state_path(cwd).is_file()


#: Parsed mutable run states, keyed by path and file identity.  Callers mutate
#: the returned state before saving it, so cache hits are deep copies rather
#: than the shared object used for immutable workflow snapshots below.
_states: Dict[str, Tuple[int, int, dict]] = {}


def load_state(cwd: Optional[str] = None) -> dict:
    path = _state_path(cwd)
    if not path.is_file():
        raise StateError(
            "no active cflow run in this directory (start one with the "
            "cflow 'start' tool or see 'claunch cflow ls')"
        )
    try:
        st = path.stat()
        key = str(path)
        hit = _states.get(key)
        if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
            return copy.deepcopy(hit[2])
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise StateError(f"corrupt cflow state at {path}: {exc}") from exc
    if not isinstance(doc, dict):
        raise StateError(f"corrupt cflow state at {path}")
    _states[key] = (st.st_mtime_ns, st.st_size, doc)
    return copy.deepcopy(doc)


def save_state(state: dict, cwd: Optional[str] = None) -> None:
    path = _state_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(state, indent=2), encoding="utf-8")
    _forget(path)


def clear_state(cwd: Optional[str] = None) -> None:
    for path in (_state_path(cwd), _snapshot_path(cwd)):
        try:
            path.unlink()
        except OSError:
            pass
        _forget(path)


#: Files that make up one run inside its slot directory (the scope's main
#: run, or one of its sub runs — see :data:`SUB_DIR`).
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
        _forget(src)
    return target


def snapshot_workflow(text: str, cwd: Optional[str] = None) -> None:
    path = _snapshot_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    _forget(path)


def _forget(path: Path) -> None:
    """Drop any parse remembered for ``path``.

    The mtime/size keys below already catch every *cross-process* rewrite
    (the daemon reading what an agent's MCP server wrote). This is for the
    one case they cannot see: a same-process archive-and-restart that puts a
    different file of the same length back at the same path inside one clock
    tick. Called from the three writers, so the caches are exact for anything
    this process does and merely eventually-right for anything it does not.
    """
    key = str(path)
    _states.pop(key, None)
    _snapshots.pop(key, None)
    _journals.pop(key, None)


#: Parsed snapshots, keyed by path -> (mtime, size, workflow). A snapshot is
#: written once at ``start`` (and again per recur round), while the daemon's
#: runs list re-reads *every* known run's snapshot on a two-second poll --
#: re-parsing ~90 YAML files a second was the single largest consumer of the
#: daemon's event loop. Keyed on the file's own mtime/size like
#: :mod:`daemon.ctxsize`'s transcript cache, so a rewritten snapshot re-parses
#: and a poll where nothing changed costs one stat. Sharing the parse is safe
#: because :class:`model.Workflow` (and every node under it) is frozen.
_snapshots: Dict[str, Tuple[int, int, model.Workflow]] = {}


def load_snapshot(cwd: Optional[str] = None, scope: Optional[str] = None) -> model.Workflow:
    path = _snapshot_path(cwd, scope)
    if not path.is_file():
        raise StateError("cflow run state exists but the workflow snapshot is missing")
    key = str(path)
    try:
        st = path.stat()
    except OSError:
        return model.load(path)  # let the reader raise the real error
    hit = _snapshots.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    workflow = model.load(path)
    _snapshots[key] = (st.st_mtime_ns, st.st_size, workflow)
    return workflow


def journal(event: str, data: Optional[Dict] = None, cwd: Optional[str] = None) -> None:
    journal_mod.append(journal_path(cwd), event, data, at=utcnow())


#: Parsed journals, keyed by path -> (mtime, size, entries). Same bargain as
#: ``_snapshots``: the runs list reads every known run's journal on a
#: two-second poll, and a finished run's journal never changes again. The
#: entries are handed out (filtered into a fresh list) rather than copied, so
#: callers must treat them as read-only -- every one of them builds new dicts
#: out of the fields it wants.
_journals: Dict[str, Tuple[int, int, List[dict]]] = {}


def _journal_entries(path: Path) -> List[dict]:
    """Every entry in ``path``, parsed at most once per write of the file."""
    try:
        st = path.stat()
    except OSError:
        return []
    key = str(path)
    hit = _journals.get(key)
    if hit is not None and hit[0] == st.st_mtime_ns and hit[1] == st.st_size:
        return hit[2]
    out = journal_mod.read(path)
    _journals[key] = (st.st_mtime_ns, st.st_size, out)
    return out


def read_journal(
    cwd: Optional[str] = None,
    scope: Optional[str] = None,
    *,
    run_id: Optional[str] = None,
    events: Optional[List[str]] = None,
) -> List[dict]:
    path = journal_path(cwd, scope)
    if not path.is_file():
        return []
    entries = _journal_entries(path)
    if run_id is None:
        selected = list(entries)
    else:
        selected = [e for e in entries if e.get("run") == run_id]
    if events is not None:
        selected = [e for e in selected if e.get("event") in set(events)]
    return selected
