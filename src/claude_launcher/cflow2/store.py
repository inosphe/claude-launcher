"""Run storage for cflow v2: ``<cwd>/.cflow2/runs/<scope>/``.

Spec: ``docs/cflow-v2.md`` section 4.1. One slot per (directory, scope); the
scope is the managed session's name (``$CLAUNCH_SESSION``) or ``default``.

Files in a slot:

- ``chart.json``    -- the IR snapshot written once at ``start``. A run reads
  only this file for its definition, so editing the source mid-run changes
  nothing.
- ``state.json``    -- the active configuration, data model, visits and
  delegations. Rewritten atomically (scratch file + rename in the same dir).
- ``journal.jsonl`` -- append-only event log, one ``{ts, event, ...}`` per line.
- ``receipts/``     -- delegation receipts (``<delegation id>.json``/``.out``).
- ``.lock``         -- held across every mutation (two processes write a run:
  the agent's MCP server and the daemon/CLI).

Finished runs are moved to ``<cwd>/.cflow2/archive/<scope>/<run_id>/``.
"""

from __future__ import annotations

import contextlib
import json
import os
import re
import shutil
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .. import atomic
from . import ir

SESSION_ENV = "CLAUNCH_SESSION"
DEFAULT_SCOPE = "default"
BASE_DIR = ".cflow2"

CHART_FILE = "chart.json"
STATE_FILE = "state.json"
JOURNAL_FILE = "journal.jsonl"
RECEIPTS_DIR = "receipts"
LOCK_FILE = ".lock"
RUN_FILES = (CHART_FILE, STATE_FILE, JOURNAL_FILE)

LOCK_TIMEOUT = 10.0
#: An O_EXCL lock file older than this is treated as abandoned (non-posix only;
#: ``flock`` locks die with their holder).
LOCK_STALE_AFTER = 120.0

#: A scope is a session name; ``~`` joins a nested delegation run's scope to
#: its parent's (``<scope>~<delegation id>``, doc 7) and ``+`` a side run's
#: name to its session's (``<session>+<side>``, doc 2.9).
_SCOPE_RE = re.compile(r"^[A-Za-z0-9._~+-]+$")
NESTED_SEP = "~"
SIDE_SEP = "+"
#: A side run's name (``sides:`` keys, doc 2.9).
SIDE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class StoreError(Exception):
    """Missing/corrupt run storage, a bad scope, or a busy lock."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def normalize_scope(scope: Optional[str]) -> str:
    """The scope as a path component (a session name), or :class:`StoreError`."""
    scope = (scope or os.environ.get(SESSION_ENV) or DEFAULT_SCOPE).strip()
    if not _SCOPE_RE.match(scope) or scope in (".", ".."):
        raise StoreError(
            f"invalid cflow2 scope {scope!r}: a scope is a session name -- "
            f"letters, digits, '.', '_', '-' (and '~' for a nested delegation run, "
            f"'+' for a side run)"
        )
    return scope


def side_scope(session: str, side: str) -> str:
    """The slot of ``session``'s side run ``side``: ``<session>+<side>``."""
    if not SIDE_NAME_RE.match(str(side or "")):
        raise StoreError(f"invalid side run name {side!r}: a letter or '_', then letters, "
                         f"digits, '_', '-'")
    return normalize_scope(f"{main_scope(session)}{SIDE_SEP}{side}")


def main_scope(scope: str) -> str:
    """The session slot a scope belongs to: ``s1`` for ``s1``, ``s1+stack``
    and a nested run under either (``s1+stack~d2``)."""
    return str(scope or "").split(NESTED_SEP, 1)[0].split(SIDE_SEP, 1)[0]


def side_of(scope: str) -> Optional[str]:
    """The side run name of a side slot (``stack`` for ``s1+stack``); None for
    a main slot. A nested run's scope reads as its top run's."""
    top = str(scope or "").split(NESTED_SEP, 1)[0]
    return top.split(SIDE_SEP, 1)[1] if SIDE_SEP in top else None


def resolve_cwd(cwd: Optional[str] = None) -> Path:
    return Path(cwd or os.getcwd()).resolve()


class RunStore:
    """The files of one (cwd, scope) slot."""

    def __init__(self, cwd: Optional[str] = None, scope: Optional[str] = None):
        self.cwd = resolve_cwd(cwd)
        self.scope = normalize_scope(scope)
        self.dir = self.cwd / BASE_DIR / "runs" / self.scope

    # -- paths -----------------------------------------------------------
    @property
    def chart_path(self) -> Path:
        return self.dir / CHART_FILE

    @property
    def state_path(self) -> Path:
        return self.dir / STATE_FILE

    @property
    def journal_path(self) -> Path:
        return self.dir / JOURNAL_FILE

    @property
    def receipts_dir(self) -> Path:
        return self.dir / RECEIPTS_DIR

    def receipt_path(self, delegation_id: str, suffix: str = ".json") -> Path:
        return self.receipts_dir / f"{delegation_id}{suffix}"

    def archive_root(self) -> Path:
        return self.cwd / BASE_DIR / "archive" / self.scope

    # -- reading ---------------------------------------------------------
    def exists(self) -> bool:
        return self.state_path.is_file()

    def load_state(self) -> Dict[str, Any]:
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise StoreError(
                f"no cflow2 run in {self.cwd} for scope {self.scope!r}"
            ) from None
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise StoreError(f"corrupt {self.state_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise StoreError(f"corrupt {self.state_path}: not an object")
        return data

    def load_chart(self) -> ir.Chart:
        try:
            raw = self.chart_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise StoreError(f"run snapshot {self.chart_path} is missing") from None
        try:
            return ir.Chart.from_json(raw)
        except (ValueError, KeyError, ir.IRError) as exc:
            raise StoreError(f"corrupt {self.chart_path}: {exc}") from exc

    def journal(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        try:
            lines = self.journal_path.read_text(encoding="utf-8").splitlines()
        except FileNotFoundError:
            return []
        out: List[Dict[str, Any]] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        if limit is not None and limit >= 0:
            out = out[-limit:] if limit else []
        return out

    # -- writing ---------------------------------------------------------
    def write_chart(self, chart: ir.Chart) -> None:
        """Snapshot the IR. Written once per run; refuses to overwrite."""
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.chart_path.exists():
            raise StoreError(f"{self.chart_path} already exists; archive the run first")
        _atomic_write(self.chart_path, chart.to_json())

    def save_state(self, state: Dict[str, Any]) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        _atomic_write(
            self.state_path, json.dumps(state, ensure_ascii=False, indent=1, default=str)
        )

    def append(self, events: List[Dict[str, Any]]) -> None:
        if not events:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            for ev in events:
                rec = dict(ev)
                rec.setdefault("ts", utcnow())
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")

    def write_receipt(self, delegation_id: str, payload: Any, suffix: str = ".json") -> Path:
        path = self.receipt_path(delegation_id, suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False, indent=1, default=str
        )
        _atomic_write(path, text)
        return path

    def archive(self, run_id: Optional[str] = None) -> Path:
        """Move the slot's run files (and receipts) to the archive."""
        if not self.exists():
            raise StoreError(f"nothing to archive in {self.dir}")
        if not run_id:
            try:
                run_id = str(self.load_state().get("run_id") or "run")
            except StoreError:
                run_id = "run"
        target = self.archive_root() / run_id
        n = 0
        while target.exists():
            n += 1
            target = self.archive_root() / f"{run_id}-{n}"
        target.mkdir(parents=True)
        for name in RUN_FILES:
            src = self.dir / name
            if src.is_file():
                shutil.move(str(src), str(target / name))
        if self.receipts_dir.is_dir():
            shutil.move(str(self.receipts_dir), str(target / RECEIPTS_DIR))
        return target

    def archived(self) -> List[Path]:
        root = self.archive_root()
        if not root.is_dir():
            return []
        return sorted(p for p in root.iterdir() if p.is_dir())

    # -- locking ---------------------------------------------------------
    @contextlib.contextmanager
    def lock(self, timeout: float = LOCK_TIMEOUT) -> Iterator[None]:
        """Hold the slot exclusively for one mutation."""
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.dir / LOCK_FILE
        try:
            import fcntl  # type: ignore
        except ImportError:  # Windows
            fcntl = None  # type: ignore
        if fcntl is not None:
            with _flock(path, fcntl, timeout):
                yield
            return
        with _excl_lock(path, timeout):
            yield


def _atomic_write(path: Path, text: str) -> None:
    with atomic.scratch(path) as tmp:
        tmp.write_text(text, encoding="utf-8")
        atomic.replace(tmp, path)


@contextlib.contextmanager
def _flock(path: Path, fcntl: Any, timeout: float) -> Iterator[None]:
    fd = os.open(str(path), os.O_CREAT | os.O_RDWR, 0o644)
    try:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except OSError:
                if time.monotonic() >= deadline:
                    raise StoreError(
                        f"another process is changing this cflow2 run "
                        f"({path.parent}); try again in a moment"
                    )
                time.sleep(0.02)
        try:
            yield
        finally:
            fcntl.flock(fd, fcntl.LOCK_UN)
    finally:
        os.close(fd)


@contextlib.contextmanager
def _excl_lock(path: Path, timeout: float) -> Iterator[None]:
    deadline = time.monotonic() + timeout
    while True:
        try:
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            break
        except FileExistsError:
            try:
                stale = (time.time() - path.stat().st_mtime) > LOCK_STALE_AFTER
            except OSError:
                stale = False
            if stale:
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if time.monotonic() >= deadline:
                raise StoreError(
                    f"another process is changing this cflow2 run "
                    f"({path.parent}); try again in a moment"
                )
            time.sleep(0.02)
    try:
        with contextlib.suppress(OSError):
            os.write(fd, f"{os.getpid()} {utcnow()}".encode("utf-8"))
        os.close(fd)
        yield
    finally:
        with contextlib.suppress(OSError):
            path.unlink()


def list_scopes(cwd: Optional[str] = None) -> List[str]:
    """Scopes in ``cwd`` that hold a run."""
    base = resolve_cwd(cwd) / BASE_DIR / "runs"
    if not base.is_dir():
        return []
    return sorted(p.name for p in base.iterdir() if (p / STATE_FILE).is_file())
