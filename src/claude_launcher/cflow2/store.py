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

A scope names its daemon: ``<session>@<daemon>`` (``s15@main``; a side run
``s15@main+stack``, a nested run ``s15@main+stack~d2``). Session names are
numbered per daemon, so two daemons with sessions in one directory both have
an ``s15``; the qualifier keeps their slots apart (claunch-9jmdz, doc 4.1).
A scope given without it is this process's daemon's (:func:`qualify`).
Slots written before the qualifier (``runs/s15``) are read in place until the
owning daemon moves them at its next start (:mod:`.slot_migration`).
"""

from __future__ import annotations

import contextlib
import functools
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

#: A scope is a session name qualified by its daemon (``<session>@<daemon>``,
#: claunch-9jmdz); ``~`` joins a nested delegation run's scope to its
#: parent's (``<scope>~<delegation id>``, doc 7) and ``+`` a side run's name
#: to its session's (``<session>@<daemon>+<side>``, doc 2.9).
_SCOPE_RE = re.compile(r"^[A-Za-z0-9._~+@-]+$")
NESTED_SEP = "~"
SIDE_SEP = "+"
DAEMON_SEP = "@"
#: A daemon id after ``@``: an instance name, or the default daemon's id.
_DAEMON_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")
#: The state key a run records its daemon under (stamped at start).
DAEMON_KEY = "daemon"
#: A side run's name (``sides:`` keys, doc 2.9).
SIDE_NAME_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_-]*$")


class StoreError(Exception):
    """Missing/corrupt run storage, a bad scope, or a busy lock."""


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def current_daemon() -> str:
    """This process's daemon id: ``$CLAUNCH_DAEMON``, or the default
    daemon's id (:data:`..daemon.paths.DEFAULT_INSTANCE_ID`)."""
    from ..daemon import paths

    try:
        return paths.instance_id()
    except ValueError as exc:
        raise StoreError(str(exc)) from None


def _head(scope: str) -> int:
    """Where a scope's session part ends: before its first ``+`` or ``~``."""
    cut = [i for i in (scope.find(SIDE_SEP), scope.find(NESTED_SEP)) if i >= 0]
    return min(cut) if cut else len(scope)


def qualify(scope: str, daemon: Optional[str] = None) -> str:
    """``scope`` with ``@<daemon>`` after its session part, unless it has one
    (``s15+stack`` -> ``s15@main+stack``). ``daemon`` defaults to this
    process's (:func:`current_daemon`). ``default`` names no session and stays
    as it is."""
    scope = str(scope or "")
    cut = _head(scope)
    head = scope[:cut]
    if not head or DAEMON_SEP in head or head == DEFAULT_SCOPE:
        return scope
    return f"{head}{DAEMON_SEP}{daemon or current_daemon()}{scope[cut:]}"


def unqualified(scope: str) -> str:
    """``scope`` without its ``@<daemon>``: the slot name written before the
    qualifier (``s15@main+stack`` -> ``s15+stack``)."""
    scope = str(scope or "")
    cut = _head(scope)
    return scope[:cut].split(DAEMON_SEP, 1)[0] + scope[cut:]


def session_of(scope: str) -> str:
    """The session that drives ``scope``: its main slot's session name, without
    the daemon (``s15`` for ``s15@main+stack~d2``)."""
    return main_scope(scope).split(DAEMON_SEP, 1)[0]


def daemon_of(scope: str) -> Optional[str]:
    """The daemon id ``scope`` names; None for a slot name without one (written
    before the qualifier, or ``default``)."""
    head = main_scope(scope)
    return head.split(DAEMON_SEP, 1)[1] if DAEMON_SEP in head else None


def is_own(scope: str, daemon: Optional[str] = None) -> bool:
    """Whether ``scope`` belongs to ``daemon`` (default: this process's). A
    slot name without a daemon could be anyone's and reads as own -- the
    behaviour before the qualifier, until the slot is moved."""
    named = daemon_of(scope)
    return named is None or named == (daemon or current_daemon())


def own_name(scope: str) -> Optional[str]:
    """``scope`` as this daemon's session manager names it: without the
    ``@<daemon>`` (``s15@main`` -> ``s15``, ``s15@main+stack`` ->
    ``s15+stack``, which names no session, as before), or None when the slot
    is another daemon's. For callers that look a slot up as a session name."""
    if not is_own(scope):
        return None
    return unqualified(scope)


def normalize_scope(scope: Optional[str]) -> str:
    """The scope as a path component (a session name qualified by its daemon),
    or :class:`StoreError`. A session name without ``@<daemon>`` is this
    process's daemon's."""
    scope = (scope or os.environ.get(SESSION_ENV) or DEFAULT_SCOPE).strip()
    if not _SCOPE_RE.match(scope) or scope in (".", ".."):
        raise StoreError(
            f"invalid cflow2 scope {scope!r}: a scope is a session name -- "
            f"letters, digits, '.', '_', '-' (then '@<daemon>', '+' for a side run, "
            f"'~' for a nested delegation run)"
        )
    head = scope[:_head(scope)]
    if DAEMON_SEP in scope[len(head):] or head.count(DAEMON_SEP) > 1:
        raise StoreError(f"invalid cflow2 scope {scope!r}: '@<daemon>' comes once, "
                         f"right after the session name")
    if DAEMON_SEP in head:
        name, daemon = head.split(DAEMON_SEP, 1)
        if not name or not _DAEMON_RE.match(daemon):
            raise StoreError(f"invalid cflow2 scope {scope!r}: '<session>@<daemon>' "
                             f"needs both parts")
    return qualify(scope)


def side_scope(session: str, side: str) -> str:
    """The slot of ``session``'s side run ``side``: ``<session>+<side>``."""
    if not SIDE_NAME_RE.match(str(side or "")):
        raise StoreError(f"invalid side run name {side!r}: a letter or '_', then letters, "
                         f"digits, '_', '-'")
    return normalize_scope(f"{main_scope(session)}{SIDE_SEP}{side}")


def main_scope(scope: str) -> str:
    """The session slot a scope belongs to: ``s1@main`` for ``s1@main``,
    ``s1@main+stack`` and a nested run under either (``s1@main+stack~d2``).
    The daemon stays: it is part of the slot (:func:`session_of` drops it)."""
    return str(scope or "").split(NESTED_SEP, 1)[0].split(SIDE_SEP, 1)[0]


def side_of(scope: str) -> Optional[str]:
    """The side run name of a side slot (``stack`` for ``s1@main+stack``); None
    for a main slot. A nested run's scope reads as its top run's."""
    top = str(scope or "").split(NESTED_SEP, 1)[0]
    return top.split(SIDE_SEP, 1)[1] if SIDE_SEP in top else None


def resolve_cwd(cwd: Optional[str] = None) -> Path:
    path = Path(cwd or os.getcwd())
    if not path.is_absolute():
        return path.resolve()
    return _resolve_absolute(str(path))


@functools.lru_cache(maxsize=256)
def _resolve_absolute(path: str) -> Path:
    # The daemon builds a RunStore per read for the same few directories;
    # resolving walks every component (a stat each) every time.
    return Path(path).resolve()


class RunStore:
    """The files of one (cwd, scope) slot."""

    def __init__(self, cwd: Optional[str] = None, scope: Optional[str] = None):
        self.cwd = resolve_cwd(cwd)
        self.scope = normalize_scope(scope)
        runs = self.cwd / BASE_DIR / "runs"
        self.dir = runs / self.scope
        #: True when the run is read from its slot written before the daemon
        #: qualifier (``runs/s15``), not moved yet (:func:`legacy_dir`).
        self.legacy = False
        old = legacy_dir(runs, self.scope)
        if old is not None and not self.dir.exists():
            self.dir = old
            self.legacy = True

    @classmethod
    def at(cls, path: Path) -> "RunStore":
        """The store over the slot directory ``path`` under the name it has on
        disk (``.cflow2/runs/<name>``), for code that walks the directories
        themselves (:mod:`.slot_migration`)."""
        st = cls.__new__(cls)
        st.cwd = Path(path).parent.parent.parent
        st.scope = Path(path).name
        st.dir = Path(path)
        st.legacy = daemon_of(st.scope) is None
        return st

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
        return self._read(checkpoint.read, self.checkpoint_path, full=full)

    def _read(self, fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
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
        # the slot directory's name: a run read from its unmoved slot keeps
        # archiving where it did
        return self.cwd / BASE_DIR / "archive" / self.dir.name

    # -- reading ---------------------------------------------------------
    def exists(self) -> bool:
        return self.state_path.is_file() or self.checkpoint_path.exists()

    def load_state(self) -> Dict[str, Any]:
        if self.checkpoint_path.exists():
            return self._read(checkpoint.read_state, self.checkpoint_path)
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
            # Shared with every reader of this checkpoint generation: read-only.
            return self._read(checkpoint.read_chart, self.checkpoint_path, ir.Chart.from_json)
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

    def journal_tail(self, limit: int) -> List[Dict[str, Any]]:
        """The last ``limit`` events for a view. ``journal(limit)`` reads a
        checkpoint whole (quick_check, every event and receipt) before it
        slices; this reads only the tail."""
        if self.checkpoint_path.exists():
            return self._read(checkpoint.read_tail, self.checkpoint_path, limit)
        return self.journal(limit)

    # -- writing ---------------------------------------------------------
    def write_chart(self, chart: ir.Chart) -> None:
        """Snapshot the IR. Written once per run; refuses to overwrite."""
        self.dir.mkdir(parents=True, exist_ok=True)
        if self.chart_path.exists() or self.checkpoint_path.exists():
            raise StoreError(f"{self.chart_path} already exists; archive the run first")
        _atomic_write(self.chart_path, chart.to_json())

    def save_state(self, state: Dict[str, Any]) -> None:
        self.commit(state, [])

    def commit(self, state: Dict[str, Any], events: List[Dict[str, Any]], *,
               touch: Optional[str] = None) -> None:
        """One durable state/event transaction. Caller holds the run lock.

        ``touch`` names a timestamp key set to now only when the commit
        changes something else (an event, or another key of the state): a
        save that would only move that stamp writes nothing, and ``state``
        keeps the committed stamp.

        Existing JSON-only runs migrate on their first successful write. A
        corrupt legacy file is never silently imported or replaced.
        """
        self.dir.mkdir(parents=True, exist_ok=True)
        creating = not self.checkpoint_path.exists()
        if touch and not creating and not events:
            previous = self._read(checkpoint.read_state, self.checkpoint_path)
            stamp = previous.get(touch)
            if stamp is not None:
                fresh = state.get(touch)
                state[touch] = stamp
                # Compact output differs from encode()'s only in whitespace,
                # and json's C encoder writes it (indent= runs the Python one).
                if _compact(state) == _compact(previous):
                    return
                state[touch] = fresh
        if touch:
            state[touch] = utcnow()
        text = checkpoint.encode(state)
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
                chart = None  # kept; checkpoint.commit verifies it under the write lock
                if (self._read(checkpoint.read_state_text, self.checkpoint_path) == text
                        and not events):
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

    def metrics_snapshot(self, limit: int = 2048, byte_limit: int = 256 * 1024) -> dict:
        """Read-only metrics input; a checkpoint never falls back to projections.

        State, chart and events share one SQLite generation. Legacy inputs
        must carry a matching started event before their journal is trusted.
        """
        if self.checkpoint_path.exists():
            try:
                snap = checkpoint.metrics_snapshot(self.checkpoint_path, limit=limit,
                                                   byte_limit=byte_limit)
            except checkpoint.CheckpointError as exc:
                raise StoreError(str(exc)) from exc
            try:
                return {**snap, "state": json.loads(snap["state"]),
                        "chart": ir.Chart.from_json(snap["chart"])}
            except (ir.IRError, KeyError, ValueError) as exc:
                raise StoreError(f"corrupt metrics snapshot {self.checkpoint_path}: {exc}") from exc
        state, chart = self.load_state(), self.load_chart()
        events, complete, size = [], True, 0
        try:
            with self.journal_path.open("rb") as fh:
                for _ in range(limit + 1):
                    line = fh.readline(min(65537, byte_limit - size + 1))
                    if not line:
                        break
                    if len(events) == limit or size + len(line) > byte_limit or not line.endswith(b"\n"):
                        complete = False
                        break
                    size += len(line)
                    event = json.loads(line)
                    if not isinstance(event, dict):
                        raise ValueError("journal event is not an object")
                    events.append(event)
        except FileNotFoundError:
            complete = False
        starts = [e.get("run") for e in events if e.get("event") == "started"]
        if starts != [state.get("run_id")]:
            events, complete = [], False
        return {"state": state, "chart": chart, "events": events,
                "complete": complete, "bytes": size}

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


def _compact(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


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


def legacy_dir(runs: Path, scope: str) -> Optional[Path]:
    """The unmoved slot ``scope`` is read from: ``runs/<scope without
    @daemon>`` when that is a real directory holding a run that is not another
    daemon's (its state names no daemon, or this one). None otherwise.

    Before its daemon moves it (:mod:`.slot_migration`) a slot is read and
    written where it is, as it was before the qualifier. A run started since
    records its daemon (:data:`DAEMON_KEY`), so a session of another daemon
    with the same name does not take it over in the meantime. A symlink is
    what a move leaves behind for processes still running the old code: the
    qualified slot is the run, the link is not read."""
    name = unqualified(scope)
    if name == scope:
        return None
    old = runs / name
    if old.is_symlink() or not old.is_dir():
        return None
    if not ((old / STATE_FILE).is_file() or (old / checkpoint.FILE).exists()):
        return None
    owner = slot_daemon(old)
    if owner is not None and owner != daemon_of(scope):
        return None
    return old


def slot_daemon(path: Path) -> Optional[str]:
    """The daemon a slot directory's run recorded at start, or None."""
    try:
        if (path / checkpoint.FILE).exists():
            state = json.loads(checkpoint.read(path / checkpoint.FILE)["state"])
        else:
            state = json.loads((path / STATE_FILE).read_text(encoding="utf-8"))
    except (checkpoint.CheckpointError, OSError, ValueError, KeyError):
        return None
    value = state.get(DAEMON_KEY) if isinstance(state, dict) else None
    return str(value) if value else None


def list_scopes(cwd: Optional[str] = None) -> List[str]:
    """Scopes in ``cwd`` that hold a run, each with its daemon.

    A slot not moved yet (``runs/s15``) is listed under the daemon its run
    recorded, else this process's daemon -- the name :class:`RunStore` reads
    it back by (:func:`legacy_dir`). The link a move leaves behind is not
    listed."""
    base = resolve_cwd(cwd) / BASE_DIR / "runs"
    if not base.is_dir():
        return []
    out = set()
    for p in base.iterdir():
        if p.is_symlink() or not ((p / STATE_FILE).is_file() or (p / checkpoint.FILE).exists()):
            continue
        name = p.name
        if daemon_of(name) is None and main_scope(name) != DEFAULT_SCOPE:
            try:
                name = qualify(name, slot_daemon(p))
            except StoreError:
                continue
        out.add(name)
    return sorted(out)
