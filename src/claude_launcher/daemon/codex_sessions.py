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
import uuid
from pathlib import Path
from typing import Iterable, Optional, Set


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
                yield (
                    str(session_id),
                    os.path.abspath(str(cwd)),
                    path.stat().st_mtime,
                    path,
                )
        except (OSError, ValueError, TypeError):
            # Codex may still be writing the first line; the polling caller
            # will see it on the next pass.
            continue


#: The subcommand codex opens an existing conversation with. Its argument list
#: is ``[SESSION_ID] [PROMPT]``, and SESSION_ID may be a uuid or a session name.
RESUME_SUBCOMMAND = "resume"


def names_conversation(args: Iterable[str]) -> Optional[str]:
    """The conversation uuid a launch's own args already name, if any.

    ``claunch new-session --harness codex -- resume <uuid>`` is a supported way
    to put a session on an existing conversation, and the uuid is then sitting
    in plain sight: there is nothing to discover and :func:`claim_new` cannot
    discover it anyway (see :func:`resumes_existing`). Reading it here is what
    keeps the definition pinned from its first spawn.

    ``None`` when the args name nothing (a bare ``resume`` opens codex's
    picker) or name a session *name* rather than a uuid -- claunch addresses
    rollouts by the uuid their ``session_meta`` carries, so a name is not
    something it can pin.
    """
    args = list(args)
    try:
        after = args[args.index(RESUME_SUBCOMMAND) + 1]
    except (ValueError, IndexError):
        return None
    try:
        uuid.UUID(after)
    except (ValueError, AttributeError, TypeError):
        return None
    return after


def resumes_existing(args: Iterable[str]) -> bool:
    """Whether these args attach to a conversation codex has already written.

    :func:`claim_new` waits for a ``session_meta`` record that was not in the
    pre-launch snapshot, which is how a *fresh* codex conversation announces
    itself. A resumed one announces nothing: codex appends to the rollout it
    already has, so no new record ever appears and the wait can only time out.
    Measured on this machine 2026-09-11 -- ``F:/works/gds6`` held one rollout
    written from 11:09:56 through 13:39:37 with three sessions attached to it,
    and the daemon log carries the claim warning for ``s507`` (12:20:59) with no
    later "discovered delayed" line, against ``s508`` (a fresh conversation)
    recovering three seconds after the same warning.

    So the caller asks this first and does not start a wait it knows cannot
    settle.
    """
    return RESUME_SUBCOMMAND in list(args)


def snapshot(config_dir: Path) -> Set[str]:
    """Return all conversation UUIDs already present in a Codex profile."""
    return {
        session_id for session_id, _cwd, _mtime, _path in (_records(config_dir) or ())
    }


def find(config_dir: Path, session_id: str) -> Optional[Path]:
    """Return the active rollout for ``session_id``, if it is present.

    The id is read from the rollout's ``session_meta`` record instead of
    inferred from its filename.  Codex currently includes the id in both,
    but the persisted record is the same identity :func:`snapshot`,
    :func:`latest`, and :func:`claim_new` already trust.
    """
    matches = [
        (mtime, path)
        for found, _cwd, mtime, path in (_records(config_dir) or ())
        if found == str(session_id)
    ]
    return max(matches, key=lambda item: item[0])[1] if matches else None


def latest(
    config_dir: Path, cwd: str, *, taken: Iterable[str] = ()
) -> Optional[str]:
    """Return the most recently written rollout for ``cwd``, if any.

    This is only the migration path for definitions created before claunch
    learned to pin Codex UUIDs, and for the ones :func:`claim_new` could not
    settle.  Once selected, the manager persists the UUID and every later
    restore is exact.

    ``taken`` is every conversation another session definition already holds,
    and rollouts in it are skipped.  A cwd is not an identity: several codex
    sessions routinely stand in one checkout, and picking by write time alone
    hands each of them whichever conversation was written last -- which is one
    conversation shared by several sessions, every one of them reopening
    somebody else's transcript and appending to it.  Measured on this machine
    2026-09-11: sessions ``s507``, ``s514`` and ``s516`` all stood in
    ``F:/works/gds6``, which held exactly one rollout, and all three ended up
    pinned to it.  Skipping what is taken makes the pick return nothing rather
    than a conversation that is already somebody's -- and nothing is the
    honest answer, because there is no rollout here that belongs to this
    session.
    """
    target = os.path.normcase(os.path.abspath(cwd))
    claimed = {str(t) for t in taken}
    matches = [
        (mtime, session_id)
        for session_id, record_cwd, mtime, _path in (_records(config_dir) or ())
        if os.path.normcase(record_cwd) == target and session_id not in claimed
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
            for session_id, record_cwd, _mtime, _path in (_records(config_dir) or ())
            if session_id not in known and os.path.normcase(record_cwd) == target
        ]
        if len(matches) == 1:
            return matches[0]
        if time.monotonic() >= deadline:
            return None
        time.sleep(poll)
