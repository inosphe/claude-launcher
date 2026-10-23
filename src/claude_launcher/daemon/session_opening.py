"""The opening a session was created with, kept so it can be handed over again.

A session's opening is composed once (``daemon.onboard.arrange`` plus the
prompt packs and the task) and handed to the harness once
(``onboard.open_with``); nothing replays it. That is right for a start that
lands, and it is the hole this module fills: a harness that never took the
paste, or dropped the Enter, leaves the agent with no idea what it exists
for -- and the only copy of what it should have read lived in the daemon's
memory for the length of the create call.

So the composed block is recorded beside the session's output log, and the
detail panel's Opening task section reads it back (``GET`` on
``/api/sessions/<name>/opening``). "Re-inject" types the same text in again
through ``Session.deliver`` with ``force=True`` -- the person pressed a
button, the same reason the dashboard's "deliver now" forces -- and "Copy"
puts it on the clipboard so it can be pasted into the terminal by hand.

The record is what it says it is: the bytes that were handed to the session
at creation, images not included (they cannot be re-sent). Sessions created
before this existed have no file; for those :func:`recompose` builds the part
that is still recorded -- the score goal line, the prompt packs and the
opening task -- and every reader says ``recomposed`` rather than presenting a
reconstruction as the original.

Nothing here changes the session definition (``SessionDef.task`` and
``prompt_packs`` stay records of their own), and nothing is replayed
automatically: an automatic re-send would duplicate an opening that did land,
and only a person can see that it did.
"""

from __future__ import annotations

import json
import logging
import os
import threading
from datetime import datetime, timezone
from typing import Optional

from .. import atomic, prompt_packs
from . import paths, score_goal

log = logging.getLogger(__name__)

#: Where the text came from, as every reply and the panel name it.
RECORDED = "recorded"
RECOMPOSED = "recomposed"
NONE = "none"

#: Refused rather than truncated: a session's opening is its own instructions,
#: and half of them read as a different job.
MAX_BYTES = 1024 * 1024

_lock = threading.RLock()


def _path(name: str):
    return paths.session_dir(name) / "opening.json"


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(name: str, text: str, *, images: int = 0, at: Optional[str] = None) -> bool:
    """Keep ``text`` as ``name``'s opening. False for an empty block.

    Called from the one place an opening exists -- ``_onboard_and_launch``,
    right after it is composed and before the harness is launched -- so a
    create that fails after this still leaves the file, and a later create
    with the same name overwrites it. ``images`` is a count, not the images:
    they were staged files claimed by the opening, and what can be handed
    over again is the text.
    """
    block = str(text or "")
    if not block.strip():
        return False
    data = json.dumps(
        {
            "text": block,
            "at": at or _now(),
            "images": int(images or 0),
            "chars": len(block),
        },
        ensure_ascii=False,
    )
    if len(data.encode("utf-8")) > MAX_BYTES:
        # Not a reason to fail a session that is about to start: the opening
        # still goes in, there is just no copy of it to hand over later.
        log.warning(
            "the opening of %r is over %d bytes; not recorded", name, MAX_BYTES
        )
        return False
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    with _lock:
        with atomic.scratch(path) as tmp:
            tmp.write_text(data, encoding="utf-8")
            atomic.replace(tmp, path)
    return True


def read(name: str) -> Optional[dict]:
    """The recorded opening for ``name``, or None when there is none."""
    path = _path(name)
    try:
        if os.stat(path).st_size > MAX_BYTES:
            return None
        raw = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("text"), str):
        return None
    text = raw["text"]
    if not text.strip():
        return None
    return {
        "text": text,
        "at": raw.get("at") or "",
        "images": int(raw.get("images") or 0),
        "chars": len(text),
    }


def recompose(sdef) -> str:
    """What is still recorded of an opening whose text was not kept.

    The order is the opening's own (``_onboard_and_launch``): the score goal
    line first, the prompt packs ahead of the task, the task last. What is
    missing is what no record can rebuild -- the parent block, the mesh
    briefing, the cflow block, the board line, uploaded files -- so callers
    label this ``recomposed`` and the panel says so.
    """
    parts = []
    if score_goal.active(sdef):
        parts.append(score_goal.prompt(sdef))
    # Recorded packs are written by prompt_packs.records, but a record edited
    # by hand is still a record: one malformed entry must not turn the whole
    # answer into a 500 when the operator is trying to recover a session.
    packs = prompt_packs.opening([
        r for r in getattr(sdef, "prompt_packs", ()) or ()
        if isinstance(r, dict) and isinstance(r.get("text"), str)
        and isinstance(r.get("id"), str)
    ])
    if packs:
        parts.append(packs)
    task = str(getattr(sdef, "task", "") or "").strip()
    if task:
        parts.append(task)
    return "\n\n".join(parts)


def view(name: str, sdef) -> dict:
    """What the panel and the routes answer: the text and where it came from."""
    found = read(name)
    if found is not None:
        return {"source": RECORDED, **found}
    text = recompose(sdef)
    if not text.strip():
        return {"source": NONE, "text": "", "at": "", "images": 0, "chars": 0}
    return {"source": RECOMPOSED, "text": text, "at": "", "images": 0, "chars": len(text)}
