"""Run state, journal, and workflow discovery for cflow.

One active run per working directory, stored under ``<cwd>/.cflow/``:

- ``workflow.yaml`` — a snapshot of the workflow taken at ``start`` (mid-run
  edits to the source file cannot corrupt a running position)
- ``state.json``   — cursor, status, approvals, pending selection
- ``journal.jsonl``— append-only event log (started, delivered, completed,
  verify results, selections, approvals, done/aborted)

Workflow files are looked up by name in the project first, then globally:
``<cwd>/.claunch/workflows/*.yaml`` → ``~/.claude-launcher/workflows/*.yaml``.
An explicit path (ending in .yaml/.yml) is used as-is.
"""

from __future__ import annotations

import json
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

from .. import config
from . import model

#: Directory (relative to a project) holding its workflow declarations.
PROJECT_WORKFLOWS = Path(".claunch") / "workflows"


class StateError(Exception):
    """Raised for missing/corrupt run state."""


def global_workflows_dir() -> Path:
    return config.launcher_home() / "workflows"


def runs_registry_path() -> Path:
    return config.launcher_home() / "cflow_runs.json"


def register_run_dir(cwd: Optional[str] = None) -> None:
    """Record this directory in the machine-local registry of cflow runs.

    The daemon web dashboard scans the registry, so runs are monitorable no
    matter where they were started (a managed session, a plain terminal, an
    orchestrator script). Best-effort: registry loss only affects listing.
    """
    target = str(Path(cwd or os.getcwd()).resolve())
    entries = [d for d in _read_registry() if d != target]
    entries.append(target)
    _write_registry(entries)


def known_run_dirs() -> List[str]:
    """Registered directories that still hold run state (pruned on read)."""
    entries = _read_registry()
    alive = [d for d in entries if (Path(d) / ".cflow" / "state.json").is_file()]
    if alive != entries:
        _write_registry(alive)
    return alive


def _read_registry() -> List[str]:
    try:
        entries = json.loads(runs_registry_path().read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    return [str(e) for e in entries if isinstance(e, str)] if isinstance(entries, list) else []


def _write_registry(entries: List[str]) -> None:
    path = runs_registry_path()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(entries, indent=2), encoding="utf-8")
    except OSError:
        pass


def cflow_dir(cwd: Optional[str] = None) -> Path:
    return Path(cwd or os.getcwd()) / ".cflow"


def _state_path(cwd: Optional[str]) -> Path:
    return cflow_dir(cwd) / "state.json"


def _snapshot_path(cwd: Optional[str]) -> Path:
    return cflow_dir(cwd) / "workflow.yaml"


def journal_path(cwd: Optional[str] = None) -> Path:
    return cflow_dir(cwd) / "journal.jsonl"


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


def snapshot_workflow(text: str, cwd: Optional[str] = None) -> None:
    path = _snapshot_path(cwd)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")


def load_snapshot(cwd: Optional[str] = None) -> model.Workflow:
    path = _snapshot_path(cwd)
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


def read_journal(cwd: Optional[str] = None, run_id: Optional[str] = None) -> List[dict]:
    path = journal_path(cwd)
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
