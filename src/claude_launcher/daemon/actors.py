"""Who did it: the actor recorded on a session's creation and on its ending.

A session record answers two questions a reader of the rail asks and the
process itself cannot: who started this one (``SessionDef.created_by``), and
who ended it (``Session.ended_by``). Both hold an *actor*, a small dict:

``{"kind": <kind>, "name"?: <session or run owner>, "via"?: <surface>,
"reason"?: <word>}``

Kinds:

- ``user``   -- a person: the dashboard (``via: web``, a cookie request) or a
  shell that is not a managed session's (``via: cli``, a bearer request with
  no actor header).
- ``agent``  -- a managed session acting through the CLI or an MCP tool from
  its own shell; ``name`` is that session.
- ``cflow``  -- a workflow run's machinery (kill-on-end, a reviewer reap, a
  v2 delegation's spawn or an orphan reap); ``name`` is the run's session.
- ``daemon`` -- the daemon on its own account (an idle hold, a deep-think
  sweep, a simulation runner); ``reason`` says which.
- ``self``   -- the session's own program ended (``/exit``, a crash); only
  an ending has it.
- ``os``     -- the machine went down around it (``manager.ended_by_os``).

The HTTP layer has one shared token, so a request names its actor itself:
:data:`HEADER` carries ``<kind>[:<name>]``. ``daemon_client`` sends
``agent:<$CLAUNCH_SESSION>`` from inside a managed session, and the daemon's
own calls to its routes send ``cflow:<owner>``. Without the header a
cookie-authenticated request is the dashboard and a bearer one is a person's
shell. That is a declaration, not proof -- the same token can send any
header -- and it is read as one: the record says who the request said it
was (see claunch-actor-declared-not-proven-vs48 for the same limit on the
mesh link routes).
"""

from __future__ import annotations

import os
from typing import Any, Mapping, Optional

HEADER = "X-Claunch-Actor"

USER = "user"
AGENT = "agent"
CFLOW = "cflow"
DAEMON = "daemon"
SELF = "self"
OS = "os"

KINDS = (USER, AGENT, CFLOW, DAEMON, SELF, OS)
#: The kinds a request may declare in :data:`HEADER`. ``self`` and ``os``
#: are observed by the exit funnel, never claimed.
REQUEST_KINDS = (USER, AGENT, CFLOW, DAEMON)

VIA_WEB = "web"
VIA_CLI = "cli"

#: request[...] key the auth middleware sets to how the request authenticated.
AUTH_VIA_KEY = "claunch_auth_via"
AUTH_COOKIE = "cookie"
AUTH_BEARER = "bearer"

_FIELDS = ("name", "via", "reason")
_MAX = 120


def make(kind: str, name: Optional[str] = None, *, via: Optional[str] = None,
         reason: Optional[str] = None) -> dict:
    actor = {"kind": kind}
    for key, value in (("name", name), ("via", via), ("reason", reason)):
        if value:
            actor[key] = str(value)[:_MAX]
    return actor


def coerce(value: Any) -> Optional[dict]:
    """A stored or supplied actor, normalised; None for anything else."""
    if not isinstance(value, Mapping):
        return None
    kind = value.get("kind")
    if kind not in KINDS:
        return None
    return make(kind, **{k: value.get(k) for k in _FIELDS if isinstance(value.get(k), str)})


def parse_header(value: Optional[str]) -> Optional[dict]:
    """``<kind>[:<name>]`` -> an actor, or None when absent or malformed."""
    value = (value or "").strip()
    if not value:
        return None
    kind, _, name = value.partition(":")
    kind = kind.strip().lower()
    if kind not in REQUEST_KINDS:
        return None
    return make(kind, name.strip() or None)


def header_value(kind: str, name: Optional[str] = None) -> str:
    return f"{kind}:{name}" if name else kind


def env_header(env: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """What ``daemon_client`` declares by default: the managed session whose
    shell this is, or nothing (a person's own shell)."""
    session = ((env if env is not None else os.environ).get("CLAUNCH_SESSION") or "").strip()
    return header_value(AGENT, session) if session else None


def from_request(request) -> dict:
    """The actor behind an API request (see the module docstring)."""
    headers = getattr(request, "headers", None) or {}
    declared = parse_header(headers.get(HEADER))
    if declared is not None:
        return declared
    try:
        via = request.get(AUTH_VIA_KEY)
    except Exception:  # noqa: BLE001 -- a test double without a mapping
        via = None
    return make(USER, via=VIA_WEB if via == AUTH_COOKIE else VIA_CLI)


def with_reason(actor: Optional[dict], reason: Optional[str]) -> Optional[dict]:
    if actor is None or not reason or actor.get("reason"):
        return actor
    return {**actor, "reason": str(reason)[:_MAX]}


def note(session, by: Optional[dict]) -> None:
    """Record who asked for ``session``'s ending before the caller signals
    it (``Session.note_ender``: the first asker wins). A stand-in session
    without the method records nothing."""
    method = getattr(session, "note_ender", None)
    if method is not None and by:
        method(by)


def label(actor: Optional[Mapping]) -> str:
    """One short phrase for logs and events: ``user (web)``, ``agent s12``."""
    actor = coerce(actor)
    if actor is None:
        return "unknown"
    text = actor["kind"]
    if actor.get("name"):
        text += f" {actor['name']}"
    if actor.get("via"):
        text += f" ({actor['via']})"
    if actor.get("reason"):
        text += f" [{actor['reason']}]"
    return text
