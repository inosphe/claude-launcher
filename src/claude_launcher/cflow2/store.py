"""Run storage for cflow v2: ``<cwd>/.cflow2/runs/<scope>/``.

Spec: ``docs/cflow-v2.md`` section 4.1. One slot per (directory, scope); the
scope is the managed session's name (``$CLAUNCH_SESSION``) or ``default``.

Files in a slot:

- ``chart.json``    -- the IR snapshot written once at ``start``. The run's
  definition is fixed at start, so editing the source mid-run changes nothing.
- ``state.json``    -- the active configuration, data model, visits and
  delegations. Rewritten atomically (scratch file + rename in the same dir).
- ``journal.jsonl`` -- append-only event log, one ``{ts, event, ...}`` per line.
- ``receipts/``     -- delegation receipts (``<delegation id>.json``/``.out``).
- ``checkpoint.sqlite3`` -- authoritative transactional state, chart, journal
  and receipt recovery data (synchronous=EXTRA, rollback journal). JSON files
  are exports after migration; never replace the checkpoint from those exports.
- ``.lock``         -- held across every mutation (two processes write a run:
  the agent's MCP server and the daemon/CLI).

Finished runs are moved to ``<cwd>/.cflow2/archive/<scope>/<run_id>/``.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import re
import shutil
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional

from .. import atomic
from . import checkpoint, ir

SESSION_ENV = "CLAUNCH_SESSION"
DEFAULT_SCOPE = "default"
BASE_DIR = ".cflow2"

CHART_FILE = "chart.json"
STATE_FILE = "state.json"
JOURNAL_FILE = "journal.jsonl"
RECEIPTS_DIR = "receipts"
LOCK_FILE = ".lock"
RUN_FILES = (CHART_FILE, STATE_FILE, JOURNAL_FILE, checkpoint.FILE,
             checkpoint.FILE + "-journal")
log = logging.getLogger(__name__)
RECOVERY_MONITOR = "storage-recovery"


def recovery_held(state: Dict[str, Any]) -> bool:
    return any(a.get("kind") == "monitor_violation" and a.get("monitor") == RECOVERY_MONITOR
               for a in state.get("approvals", []))

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
    def checkpoint_path(self) -> Path:
        return self.dir / checkpoint.FILE

    def _checkpoint(self, *, full: bool = False):
        try:
            return checkpoint.read(self.checkpoint_path, full=full)
        except checkpoint.CheckpointError as exc:
            raise StoreError(f"{exc}; preserve this run and inspect with "
                             f"claunch cflow2 recover --scope {self.scope}") from exc

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
        return self.state_path.is_file() or self.checkpoint_path.exists()

    def load_state(self) -> Dict[str, Any]:
        if self.checkpoint_path.exists():
            return json.loads(self._checkpoint()["state"])
        try:
            raw = self.state_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise StoreError(
                f"no cflow2 run in {self.cwd} for scope {self.scope!r}"
            ) from None
        except UnicodeError as exc:
            raise StoreError(f"corrupt {self.state_path}: {exc}") from exc
        try:
            data = json.loads(raw)
        except ValueError as exc:
            raise StoreError(f"corrupt {self.state_path}: {exc}") from exc
        if not isinstance(data, dict):
            raise StoreError(f"corrupt {self.state_path}: not an object")
        return data

    def load_chart(self) -> ir.Chart:
        if self.checkpoint_path.exists():
            return ir.Chart.from_json(self._checkpoint()["chart"])
        try:
            raw = self.chart_path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise StoreError(f"run snapshot {self.chart_path} is missing") from None
        try:
            return ir.Chart.from_json(raw)
        except (ValueError, KeyError, ir.IRError) as exc:
            raise StoreError(f"corrupt {self.chart_path}: {exc}") from exc

    def journal(self, limit: Optional[int] = None) -> List[Dict[str, Any]]:
        if self.checkpoint_path.exists():
            out = [json.loads(line) for line in self._checkpoint(full=True)["journal"].splitlines()]
            return out[-limit:] if limit else ([] if limit == 0 else out)
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
        if self.chart_path.exists() or self.checkpoint_path.exists():
            raise StoreError(f"{self.chart_path} already exists; archive the run first")
        _atomic_write(self.chart_path, chart.to_json())

    def save_state(self, state: Dict[str, Any]) -> None:
        self.commit(state, [])

    def commit(self, state: Dict[str, Any], events: List[Dict[str, Any]]) -> None:
        """One durable state/event transaction. Caller holds the run lock.

        Existing JSON-only runs migrate on their first successful write. A
        corrupt legacy file is never silently imported or replaced.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        text = checkpoint.encode(state)
        creating = not self.checkpoint_path.exists()
        legacy = []
        receipts = {}
        try:
            if creating:
                if self.state_path.exists():
                    self.load_state()  # refuse an already corrupt legacy run
                chart = self.chart_path.read_text(encoding="utf-8")
                ir.Chart.from_json(chart)
                if self.journal_path.exists():
                    legacy = [json.loads(line) for line in
                              self.journal_path.read_text(encoding="utf-8").splitlines()
                              if line.strip()]
                    if not all(isinstance(e, dict) for e in legacy):
                        raise ValueError("legacy journal contains a non-object")
                receipts = {p.name: p.read_text(encoding="utf-8")
                            for p in self.receipts_dir.glob("*") if p.is_file()}
            else:
                previous = self._checkpoint()
                chart = previous["chart"]
                if previous["state"] == text and not events:
                    return
            stamped = [dict(ev, ts=ev.get("ts", utcnow())) for ev in events]
            checkpoint.commit(self.checkpoint_path, text, chart, stamped,
                              legacy=legacy, receipts=receipts)
        except (checkpoint.CheckpointError, OSError, ValueError) as exc:
            raise StoreError(f"cannot commit run {self.scope}: {exc}") from exc
        # The database has committed. Projection failure must not turn a
        # successful operation into a retry (and repeat its external effects).
        try:
            _atomic_write(self.state_path, text)
            self._append_json(stamped)
        except OSError:
            log.exception("committed cflow2 checkpoint; JSON export failed in %s; "
                          "use claunch cflow2 recover --scope %s", self.dir, self.scope)

    def append(self, events: List[Dict[str, Any]]) -> None:
        if not events:
            return
        self.commit(self.load_state(), events)

    def _append_json(self, events: List[Dict[str, Any]]) -> None:
        if not events:
            return
        self.dir.mkdir(parents=True, exist_ok=True)
        with open(self.journal_path, "a", encoding="utf-8") as fh:
            for ev in events:
                rec = dict(ev)
                rec.setdefault("ts", utcnow())
                fh.write(json.dumps(rec, ensure_ascii=False, default=str) + "\n")
            fh.flush()
            os.fsync(fh.fileno())

    def write_receipt(self, delegation_id: str, payload: Any, suffix: str = ".json") -> Path:
        path = self.receipt_path(delegation_id, suffix)
        path.parent.mkdir(parents=True, exist_ok=True)
        text = payload if isinstance(payload, str) else json.dumps(
            payload, ensure_ascii=False, indent=1, default=str
        )
        if self.checkpoint_path.exists():
            try:
                checkpoint.receipt(self.checkpoint_path, path.name, text)
            except checkpoint.CheckpointError as exc:
                raise StoreError(str(exc)) from exc
        _atomic_write(path, text)
        return path

    def recovery(self, *, apply: bool = False, expected: Optional[int] = None) -> Dict[str, Any]:
        """Inspect a checkpoint, or preserve evidence and restore under the lock.

        Recovery never replays workflow commands. An applied recovery adds the
        existing monitor halt so timers and agents wait for a person's resume.
        """
        if not self.dir.is_dir():
            raise StoreError(f"no cflow2 run in {self.cwd} for scope {self.scope!r}")
        with self.lock():
            if not self.checkpoint_path.exists():
                raise StoreError("no durable checkpoint for this legacy run; preserve chart.json, "
                                 "state.json, journal.jsonl and receipts. Journal events alone "
                                 "cannot reconstruct complete state; restore a known-good backup "
                                 "or start a new run after reviewing external effects")
            snap = self._checkpoint(full=True)
            state = json.loads(snap["state"])
            chart = ir.Chart.from_json(snap["chart"])
            if (not isinstance(state, dict) or not state.get("run_id")
                    or state.get("workflow") != chart.name or state.get("scope") != self.scope
                    or not isinstance(state.get("inst"), dict)):
                raise StoreError("checkpoint state/chart identity is invalid; recovery refused")
            differences = []
            files = {STATE_FILE: snap["state"], CHART_FILE: snap["chart"],
                     JOURNAL_FILE: snap["journal"]}
            files.update({f"{RECEIPTS_DIR}/{name}": body
                          for name, body in snap["receipts"].items()})
            for name, body in files.items():
                try:
                    matches = (self.dir / name).read_text(encoding="utf-8") == body
                except (OSError, UnicodeError):
                    matches = False
                if not matches:
                    differences.append(name)
            result = {"scope": self.scope, "run": state["run_id"],
                      "revision": snap["revision"], "updated": state.get("updated"),
                      "active": state.get("active", []), "differences": differences,
                      "applied": False, "checkpoint": str(self.checkpoint_path)}
            if not apply:
                return result
            if expected is None or expected != snap["revision"]:
                raise StoreError("checkpoint changed or --revision missing; inspect recover again")
            evidence = self.dir / "recovery" / uuid.uuid4().hex
            evidence.mkdir(parents=True)
            for name in files:
                src = self.dir / name
                if src.is_file():
                    dest = evidence / name
                    dest.parent.mkdir(parents=True, exist_ok=True)
                    with dest.open("wb") as fh:
                        fh.write(src.read_bytes())
                        fh.flush()
                        os.fsync(fh.fileno())
            # Keep the validated pre-recovery checkpoint data even if publishing
            # the repaired files fails. Do not overwrite any previous evidence.
            _atomic_write(evidence / "checkpoint.json", checkpoint.encode(snap))
            holds = state.setdefault("approvals", [])
            if not any(a.get("monitor") == "storage-recovery" for a in holds):
                holds.append({"kind": "monitor_violation", "monitor": "storage-recovery",
                              "instance": None})
            if state.get("status") in ("running", "waiting_approval"):
                state.update(status="waiting_approval", reason="monitor_violation")
            self.commit(state, [{"event": "storage_recovered", "revision": snap["revision"],
                                 "evidence": str(evidence), "ts": utcnow()}])
            repaired = self._checkpoint(full=True)
            files.update({STATE_FILE: repaired["state"], JOURNAL_FILE: repaired["journal"]})
            for name, body in files.items():
                _atomic_write(self.dir / name, body)
            result.update(applied=True, evidence=str(evidence), halted="storage-recovery")
            return result

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
        for name in ("recovery",):
            if (self.dir / name).is_dir():
                shutil.move(str(self.dir / name), str(target / name))
        if self.receipts_dir.is_dir():
            shutil.move(str(self.receipts_dir), str(target / RECEIPTS_DIR))
        return target

    def archived(self) -> List[Path]:
        root = self.archive_root()
        if not root.is_dir():
            return []
        return sorted(p for p in root.iterdir() if p.is_dir())

    def start_events(self) -> List[Dict[str, Any]]:
        """Start evidence for a run history, including events outside the tail.

        Old JSON-only runs are scanned up to 1 MiB; absent evidence stays
        unknown. Checkpoint-backed runs inspect the first 2048 events and
        select at most eight matches.
        """
        names = ["started", "round_auto_started"]
        if self.checkpoint_path.exists():
            try:
                return checkpoint.read_events(self.checkpoint_path, names, 8)
            except checkpoint.CheckpointError as exc:
                raise StoreError(str(exc)) from exc
        out = []
        try:
            with self.journal_path.open("rb") as fh:
                raw = fh.read(1024 * 1024)
                # Do not parse a partial final line when the scan was capped.
                if fh.read(1):
                    raw = raw.rsplit(b"\n", 1)[0] if b"\n" in raw else b""
        except FileNotFoundError:
            return []
        for line in raw.splitlines():
            try:
                event = json.loads(line)
            except (ValueError, UnicodeError):
                continue
            if isinstance(event, dict) and event.get("event") in names:
                out.append(event)
                if len(out) == 8:
                    break
        return out

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
        with tmp.open("w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
            fh.flush()
            os.fsync(fh.fileno())
        atomic.replace(tmp, path)
        if os.name != "nt":
            fd = os.open(str(path.parent), os.O_RDONLY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)


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
    return sorted(p.name for p in base.iterdir()
                  if (p / STATE_FILE).is_file() or (p / checkpoint.FILE).exists())
