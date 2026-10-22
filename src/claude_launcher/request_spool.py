"""Write requests a session could not hand to the daemon, kept until it can.

A managed session's shell never starts a daemon (claunch-19liw.2.1): the
daemon it starts inherits that shell's environment and acts under its name
for every later generation. So when the daemon cannot be reached from a
session, a write that is safe to apply late (:func:`spoolable`) is written
here with a request id instead, and the daemon applies it when it boots or
next drains (``daemon/spool_drain.py``). Every other call fails.

The request id travels on the live request as well (:data:`HEADER`). A
request that timed out may still have been applied by a daemon that was only
slow; the daemon records the ids it applied in :class:`SeenIds`, a file that
outlives restarts, and skips a spooled copy whose id it has already seen.

Layout under the instance's daemon directory::

    spool/<session>/<request id>.json   one pending request
    spool/failed/<request id>.json      one the daemon could not apply
    spool/seen.jsonl                    ids already applied

Stdlib only: the CLI imports this on its hot path.
"""

from __future__ import annotations

import json
import os
import re
import time
import uuid
from pathlib import Path
from typing import Iterator, Optional

from .daemon import paths

#: The request header that carries a write's id to the daemon.
HEADER = "X-Claunch-Request-Id"

#: Writes that may be applied after the caller has moved on. Only mesh send
#: for now (342 of the 504 session-shell spawns, claunch-19liw.2): a message
#: is still worth delivering late. A kill, a spawn or a cflow report applied
#: minutes later is a different act from the one asked for, so those fail
#: instead and the caller decides again.
_SPOOLABLE = (
    ("POST", re.compile(r"^/api/mesh/[^/?]+/messages$")),
)

#: How long an applied id is remembered. A spooled copy older than this would
#: have been drained long before; the bound keeps the file from growing.
SEEN_TTL = 7 * 24 * 3600.0

_SAFE = re.compile(r"[^A-Za-z0-9_.-]")


def spoolable(method: str, path: str) -> bool:
    path = path.split("?", 1)[0]
    return any(m == method and rx.match(path) for m, rx in _SPOOLABLE)


def new_request_id() -> str:
    return f"rq-{uuid.uuid4().hex}"


def root() -> Path:
    return paths.daemon_dir() / "spool"


def _session_dir(session: str) -> Path:
    return root() / (_SAFE.sub("_", session) or "_")


def _write_atomic(path: Path, doc: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(doc, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, path)


def put(
    session: str, method: str, path: str, body: Optional[dict], request_id: str
) -> Path:
    """Record one pending request; returns the file it went to."""
    now = time.time()
    doc = {
        "id": request_id,
        "session": session,
        "method": method,
        "path": path,
        "body": body if body is not None else {},
        "queued_ts": now,
        "queued_at": time.strftime("%Y-%m-%dT%H:%M:%S%z", time.localtime(now)),
    }
    target = _session_dir(session) / f"{request_id}.json"
    _write_atomic(target, doc)
    return target


def pending() -> Iterator[tuple[Path, dict]]:
    """Every pending request, oldest first. Unreadable files are skipped
    (a writer between its temp file and the rename, or a damaged one)."""
    base = root()
    if not base.is_dir():
        return iter(())
    rows = []
    for d in base.iterdir():
        if not d.is_dir() or d.name == "failed":
            continue
        for f in d.glob("rq-*.json"):
            try:
                doc = json.loads(f.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            if isinstance(doc, dict) and doc.get("id"):
                rows.append((float(doc.get("queued_ts") or 0.0), f, doc))
    rows.sort(key=lambda r: r[0])
    return iter([(f, doc) for _, f, doc in rows])


def fail(path: Path, doc: dict, error: str) -> Path:
    """Move a request the daemon could not apply aside, with the reason.
    Kept, not deleted: the sender was told it would be delivered."""
    target = root() / "failed" / path.name
    _write_atomic(target, {**doc, "error": error, "failed_ts": time.time()})
    try:
        path.unlink()
    except OSError:
        pass
    return target


class SeenIds:
    """Request ids the daemon has applied, on disk so a restart keeps them."""

    def __init__(self, path: Optional[Path] = None, *, ttl: float = SEEN_TTL):
        self.path = path if path is not None else root() / "seen.jsonl"
        self.ttl = ttl
        self._ids: dict[str, float] = {}
        self._load()

    def _load(self) -> None:
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        cutoff = time.time() - self.ttl
        for line in lines:
            try:
                row = json.loads(line)
                rid, ts = str(row["id"]), float(row["ts"])
            except (ValueError, KeyError, TypeError):
                continue
            if ts >= cutoff:
                self._ids[rid] = ts
        if len(self._ids) < len(lines):
            self._rewrite()

    def _rewrite(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_name(f".{self.path.name}.{os.getpid()}.tmp")
        tmp.write_text(
            "".join(json.dumps({"id": i, "ts": t}) + "\n" for i, t in self._ids.items()),
            encoding="utf-8",
        )
        os.replace(tmp, self.path)

    def __contains__(self, request_id: str) -> bool:
        return request_id in self._ids

    def add(self, request_id: str) -> None:
        if not request_id or request_id in self._ids:
            return
        ts = time.time()
        self._ids[request_id] = ts
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"id": request_id, "ts": ts}) + "\n")
