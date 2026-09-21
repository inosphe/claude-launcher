"""Where claude keeps a conversation on disk, and how to carry one to a new cwd.

Claude Code stores transcripts **per working directory**: a conversation lives
at ``<CLAUDE_CONFIG_DIR>/projects/<slug>/<conversation-id>.jsonl``, where
``<slug>`` is the absolute working directory with every character outside
``[A-Za-z0-9]`` replaced by ``-`` (``F:\\works\\x`` -> ``F--works-x``). Resume
looks the conversation up under the slug of the directory it is resumed *in* —
which is why a session relaunched somewhere else finds nothing, and why moving
the one file is the whole trick: the jsonl itself carries no cwd check, so a
conversation whose file stands under the new directory's slug resumes there and
appends there, same id and all. (Verified against claude 2.1.237.)

This module is that trick, spelled carefully: compute the slug the same way
claude does, find the file even if our spelling of the *old* cwd disagrees with
claude's (search by conversation id — it is a uuid, unique across the config
dir), and move it. It knows nothing about sessions; the daemon's
:meth:`SessionManager.migrate` is the caller that ties it to one.
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Optional

from . import atomic

#: Everything a slug keeps; the rest becomes ``-``. This is claude's own rule,
#: read off the directories it makes — change it only against evidence.
_SLUG_RE = re.compile(r"[^A-Za-z0-9]")


def project_slug(cwd: str) -> str:
    """The directory name claude files ``cwd``'s conversations under."""
    return _SLUG_RE.sub("-", os.path.abspath(cwd))


def project_dir(config_dir: Path, cwd: str) -> Path:
    """Where conversations held in ``cwd`` live, for this config dir."""
    return Path(config_dir) / "projects" / project_slug(cwd)


def find(config_dir: Path, conversation_id: str) -> Optional[Path]:
    """The transcript of ``conversation_id``, wherever it is filed.

    A fallback for when the expected slug and claude's disagree (a path
    claude normalized differently than :func:`project_slug` predicts). The id
    is a uuid, so one match is the match; scanning tens of project dirs for
    one filename is cheap enough to be the safety net.

    An empty file is not a match. Claude creates the jsonl and writes the
    first turn into it, so a zero-byte one is a conversation caught mid-birth
    or a write that failed; ``--resume`` of it fails the same way ``--resume``
    of a missing file does. Callers ask this to find out whether there is
    something to reopen, and "a name with nothing behind it" is not.
    """
    root = Path(config_dir) / "projects"
    if not root.is_dir():
        return None
    name = f"{conversation_id}.jsonl"
    try:
        for child in root.iterdir():
            candidate = child / name
            if _has_content(candidate):
                return candidate
    except OSError:
        return None
    return None


def _has_content(path: Path) -> bool:
    """Whether ``path`` is a file with at least one byte in it.

    The size is asked, and nothing beyond it. A readability probe would fail
    on a file another process holds and a file this process could in fact
    read a moment later, and a wrong "not resumable" is the expensive error
    here -- it takes the fork off the form with a reason that is not true,
    while a wrong "resumable" costs one child that exits loudly with claude's
    own "No conversation found with session ID" (the trade :func:`exists` and
    :func:`claude_launcher.spawn._on_disk` both spell out). Zero bytes is the
    one case that needs no probe to be sure of.
    """
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def exists(config_dir: Path, conversation_id: str, cwd: str) -> bool:
    """Whether ``conversation_id`` has a transcript on disk at all.

    The question a *restore* has to ask before it runs ``--resume <id>``: a
    conversation claude never wrote is not resumable, and asking for one is
    fatal -- claude prints "No conversation found with session ID" and exits.
    A session created seconds before a daemon restart is exactly that case;
    its first turn had not landed yet, so there is no jsonl to reopen.

    Deliberately *generous*: the strict address (:func:`project_dir` of
    ``cwd``) decides it, and when that misses the whole config dir is
    searched by id (:func:`find`). The two answers fail in opposite
    directions and only one of them is cheap. Saying "resumable" when claude
    disagrees costs a failed restore -- what already happens today. Saying
    "not resumable" about a conversation that *does* exist would send the
    caller off to start a fresh one at that id, and a scrollback is not worth
    gambling on a slug spelling. So a match anywhere is a yes.

    Generous about *where*, not about *whether*: an empty jsonl is no more
    resumable than a missing one, and :func:`_has_content` is what draws that
    line. The check stops at the byte count on purpose -- see that function
    for why a corrupt or locked transcript is still answered yes.
    """
    if _has_content(project_dir(config_dir, cwd) / f"{conversation_id}.jsonl"):
        return True
    return find(config_dir, conversation_id) is not None


def relocate(
    config_dir: Path, conversation_id: str, old_cwd: str, new_cwd: str
) -> Optional[Path]:
    """Move one conversation's transcript from ``old_cwd``'s slug to
    ``new_cwd``'s, returning where it now is — or ``None`` when there is no
    transcript to move (a session that never wrote one; resuming it was
    already broken, and moving nothing does not break it further).

    A *move*, not a copy: a copy left behind is the same conversation growing
    two divergent histories, one per directory, and whichever the user finds
    later reads as the session having lost work. The destination is replaced
    if something already sits there — the file being moved is the live
    conversation, so anything at its new address is stale by definition.
    """
    src = project_dir(config_dir, old_cwd) / f"{conversation_id}.jsonl"
    if not src.is_file():
        src = find(config_dir, conversation_id)
        if src is None:
            return None
    dest = project_dir(config_dir, new_cwd) / src.name
    if src == dest:
        return dest
    dest.parent.mkdir(parents=True, exist_ok=True)
    atomic.replace(src, dest)
    return dest
