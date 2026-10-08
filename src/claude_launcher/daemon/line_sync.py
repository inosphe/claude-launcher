"""The session line's shared mode: one composer for every browser.

The web terminal's session line ("type for this session") is, by default, a
field each browser keeps to itself (``local``): what one viewer types stays
in that viewer's page and costs nothing on the wire. A session may instead
be switched to ``remote`` -- a property of the *session*, not of a viewer --
and then every page showing that session types into one shared text held
here, and every change reaches every attached viewer as a ``line_sync``
frame on the terminal socket (``daemon/ws.py``).

What is shared lives in this process's memory and nowhere else: it survives
a viewer closing its window and coming back (the next socket opens with the
state), and it ends with the daemon. Nothing is written to disk on purpose --
the line is a draft, and a draft from before a restart typed at a session
that has since moved on is worse than none.

Concurrency is last writer wins on the whole text. Two people typing in the
same instant can lose a keystroke to each other; the ``rev`` counter lets a
page tell its own echo from somebody else's write, which is all a single
shared line needs. The one write that must not race is the clear after a
send: ``if_text`` makes it conditional, so a line another viewer started
while the send was in flight is not wiped by the page that sent the old one.
"""

from __future__ import annotations

import dataclasses
from datetime import datetime, timezone
from typing import Dict, Optional, Tuple

LOCAL = "local"
REMOTE = "remote"
MODES = (LOCAL, REMOTE)

#: Ceiling on the shared text. The line is a prompt, not a document; a cap
#: keeps one runaway paste from being fanned out to every viewer on each key.
MAX_TEXT = 64 * 1024


@dataclasses.dataclass
class LineState:
    mode: str = LOCAL
    text: str = ""
    rev: int = 0
    at: str = ""
    #: The page that made the last change (its own random id), so that page
    #: can recognise the echo of its own write.
    client: str = ""

    def frame(self) -> dict:
        return {
            "type": "line_sync",
            "mode": self.mode,
            "text": self.text,
            "rev": self.rev,
            "at": self.at,
            "client": self.client,
        }


_states: Dict[str, LineState] = {}


class LineSyncError(ValueError):
    pass


class NotRemote(LineSyncError):
    """A text write to a session whose line is not shared."""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds").replace("+00:00", "Z")


def get(name: str) -> LineState:
    """The session's state; a fresh ``local`` one for a session never set."""
    return _states.get(name) or LineState()


def forget(name: str) -> None:
    _states.pop(name, None)


def reset() -> None:
    """Drop every session's state (tests)."""
    _states.clear()


def _bump(state: LineState, client: str) -> None:
    state.rev += 1
    state.at = _now()
    state.client = client


def set_mode(name: str, mode: str, *, client: str = "") -> Tuple[LineState, bool]:
    """Switch the session's line to ``mode``. The shared text is kept across
    a switch either way: turning sync off and on again gives back the line
    that was being written."""
    if mode not in MODES:
        raise LineSyncError("'mode' must be one of " + ", ".join(MODES))
    state = _states.setdefault(name, LineState())
    if state.mode == mode:
        return state, False
    state.mode = mode
    _bump(state, client)
    return state, True


def set_text(
    name: str,
    text: str,
    *,
    client: str = "",
    if_text: Optional[str] = None,
) -> Tuple[LineState, bool]:
    """Replace the shared text. Refused (``LineSyncError``) while the
    session is ``local`` -- there is nothing shared to write into then.
    ``if_text`` makes it conditional: applied only while the current text is
    exactly that, otherwise the state is returned unchanged."""
    if not isinstance(text, str):
        raise LineSyncError("'text' must be a string")
    if len(text) > MAX_TEXT:
        raise LineSyncError(f"'text' is longer than {MAX_TEXT} characters")
    state = _states.get(name)
    if state is None or state.mode != REMOTE:
        raise NotRemote("the session line is not in remote-sync mode")
    if if_text is not None and state.text != if_text:
        return state, False
    if state.text == text:
        return state, False
    state.text = text
    _bump(state, client)
    return state, True


def publish(session, state: LineState) -> int:
    """Send ``state`` to every viewer attached to ``session``. Returns how
    many sockets it went to."""
    announce = getattr(session, "announce_line_sync", None)
    if announce is None:
        return 0
    return announce(state.frame())
