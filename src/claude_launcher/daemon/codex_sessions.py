"""Discover the conversation Codex creates for a managed session.

Codex chooses its own UUID; unlike Claude it has no fresh-session flag that
lets claunch choose one up front.  Its rollout starts with a small
``session_meta`` JSON record, so the launcher snapshots the profile's known
rollouts before spawn and claims the new record for the requested cwd after
spawn.  The claimed UUID is then persisted in :class:`SessionDef` and future
restores can name it exactly instead of relying on cwd-relative ``--last``.
"""

from __future__ import annotations

import json
import os
import time
from pathlib import Path
from typing import Optional, Set


def _records(config_dir: Path):
    root = config_dir / "sessions"
    if not root.is_dir():
        return
    for path in root.glob("**/*.jsonl"):
        try:
            with path.open("r", encoding="utf-8") as stream:
                record = json.loads(stream.readline())
            payload = record.get("payload") or {}
            session_id = payload.get("id") or payload.get("session_id")
            cwd = payload.get("cwd")
            if record.get("type") == "session_meta" and session_id and cwd:
                yield str(session_id), os.path.abspath(str(cwd)), path.stat().st_mtime
        except (OSError, ValueError, TypeError):
            # Codex may still be writing the first line; the polling caller
            # will see it on the next pass.
            continue


def snapshot(config_dir: Path) -> Set[str]:
    """Return all conversation UUIDs already present in a Codex profile."""
    return {session_id for session_id, _cwd, _mtime in (_records(config_dir) or ())}


def latest(config_dir: Path, cwd: str) -> Optional[str]:
    """Return the most recently written rollout for ``cwd``, if any.

    This is only the migration path for definitions created before claunch
    learned to pin Codex UUIDs.  Once selected, the manager persists the UUID
    and every later restore is exact.
    """
    target = os.path.normcase(os.path.abspath(cwd))
    matches = [
        (mtime, session_id)
        for session_id, record_cwd, mtime in (_records(config_dir) or ())
        if os.path.normcase(record_cwd) == target
    ]
    return max(matches)[1] if matches else None


def claim_new(
    config_dir: Path,
    cwd: str,
    known: Set[str],
    *,
    timeout: float = 2.0,
    poll: float = 0.02,
) -> Optional[str]:
    """Claim the rollout created after ``known`` for exactly ``cwd``."""
    target = os.path.normcase(os.path.abspath(cwd))
    deadline = time.monotonic() + timeout
    while True:
        matches = [
            session_id
            for session_id, record_cwd, _mtime in (_records(config_dir) or ())
            if session_id not in known and os.path.normcase(record_cwd) == target
        ]
        if len(matches) == 1:
            return matches[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)
