"""Append-only execution journal primitives shared by claunch components."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Dict, Iterable, List, Optional


def append(path: Path, event: str, data: Optional[Dict] = None, *, at: str) -> dict:
    """Append one event and return the entry written to disk."""
    entry = {"at": at, "event": event}
    if data:
        entry.update(data)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(entry, ensure_ascii=False) + "\n")
    return entry


def read(path: Path, *, run_id: Optional[str] = None,
         events: Optional[Iterable[str]] = None) -> List[dict]:
    """Read valid entries, optionally filtered by run id and event names."""
    if not path.is_file():
        return []
    wanted = set(events) if events is not None else None
    out: List[dict] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        try:
            entry = json.loads(line)
        except (TypeError, ValueError):
            continue
        if not isinstance(entry, dict):
            continue
        if run_id is not None and entry.get("run") != run_id:
            continue
        if wanted is not None and entry.get("event") not in wanted:
            continue
        out.append(entry)
    return out
