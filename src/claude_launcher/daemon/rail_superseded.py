"""A session's superseded-commit verdicts, kept beside its meta.json.

:func:`claude_launcher.session_commits.supersede` decides which of a
session's unmerged commits a merged one already carries (a rebased-away
hash, claunch-rqtpe) and returns the record of what it read: each commit's
diff fingerprint and each verdict. Neither ever changes, so the record is
written to the session's directory and read back after a daemon restart;
a commit is looked at once for the life of the session's record.

Both the live list and an exited session's kept chips go through
:func:`mark`, so a card does not lose its strike when its session exits.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Callable, Dict, Optional, Tuple

from .. import atomic
from . import paths

#: The name of the file the record lives in, in the session's directory.
FILE = "rail-superseded.json"

#: More sessions than this and the table starts over.
MAX_ROWS = 4096

_lock = threading.Lock()
#: name -> record (or None), read off disk once.
_held: Dict[str, Optional[dict]] = {}


def forget() -> None:
    """Drop every held record. For tests."""
    with _lock:
        _held.clear()


def _path(name: str) -> Path:
    return paths.session_dir(name) / FILE


def _load(name: str) -> Optional[dict]:
    try:
        record = json.loads(_path(name).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    return record if isinstance(record, dict) else None


def _save(name: str, record: dict) -> None:
    path = _path(name)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with atomic.scratch(path) as tmp:
            tmp.write_text(json.dumps(record), encoding="utf-8")
            atomic.replace(tmp, path)
    except OSError:
        pass


def mark(name: str,
         judge: Callable[[Optional[dict]], Tuple[Optional[dict], bool]]) -> None:
    """Run ``judge`` on ``name``'s record and keep what it returns.

    ``judge`` is a :func:`session_commits.supersede` call (or
    :func:`session_commits.rail_superseded`) with everything but the record
    bound; it marks the chips in place.
    """
    if not name:
        return
    with _lock:
        known = name in _held
        record = _held.get(name)
    if not known:
        record = _load(name)
    record, changed = judge(record)
    if changed and record is not None:
        _save(name, record)
    with _lock:
        if len(_held) >= MAX_ROWS:
            _held.clear()
        _held[name] = record
