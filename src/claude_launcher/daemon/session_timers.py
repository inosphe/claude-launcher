"""The timers a session waits on, for the terminal's timer line (claunch-7dzae).

A session waits on more than one clock: a cflow v2 choice held until its
option's ``interval`` window opens (improv-worker-v2's await-landing ``nudge``,
ten minutes between two nudges), a v2 ``wait`` with ``seconds``, a loop's
``every`` pause, an ask's responder deadline. Each lives in a different store
and was readable only from the run page or ``flow_status``. The line above a
session's terminal (``#term-timers``, ``GET /api/sessions/<name>/timers``)
shows them together, one countdown each.

The line does not know any of those stores. A *provider* answers for one
source: it takes a :class:`Context` and returns timers built with
:func:`timer`. :data:`PROVIDERS` holds them by source name and
:func:`collect` asks every one, so a new clock joins the line by registering
a provider (:func:`register`) -- the endpoint and the strip stay as they are.

A timer is a plain dict:

- ``source`` -- the provider's name (``cflow2``).
- ``kind`` -- what sort of clock, inside that source (``hold``, ``window``,
  ``wait``, ``pause``, ``ask``). The strip picks its glyph by it and shows an
  unknown kind with the default one.
- ``id`` -- stable across polls, unique inside the source.
- ``label`` -- the one short name the strip shows (the step, the option).
- ``due`` -- epoch seconds when it fires; required.
- ``since`` -- epoch seconds when it started, or ``None``; with ``due`` the
  strip draws how far along it is.
- ``state`` -- ``waiting`` (it fires on its own), ``held`` (a pick is queued
  behind it and is applied when it fires) or ``closed`` (an option that cannot
  be taken before then; nothing is queued).
- ``detail`` -- one line for the tooltip.
- ``scope`` -- for run-backed timers, the run slot it was read from.
- ``link`` -- optional dashboard route (``#/...``) the strip opens on click.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Mapping, Optional

log = logging.getLogger(__name__)

STATE_WAITING = "waiting"
STATE_HELD = "held"
STATE_CLOSED = "closed"
STATES = (STATE_WAITING, STATE_HELD, STATE_CLOSED)


@dataclass
class Context:
    """What a provider gets to answer for one session."""

    session: str
    #: The session's directory, canonical ('' when it has none).
    cwd: str
    now: float
    #: The daemon's app mapping, for providers that read a live component
    #: (a clock object) instead of a file. Empty in tests.
    app: Mapping[str, Any] = field(default_factory=dict)


Provider = Callable[[Context], List[dict]]

#: source name -> provider, in the order they were registered.
PROVIDERS: Dict[str, Provider] = {}


def register(source: str, provider: Optional[Provider] = None):
    """Add ``provider`` under ``source`` (replacing one of the same name).
    Usable as ``@register("name")``."""
    def add(fn: Provider) -> Provider:
        PROVIDERS[source] = fn
        return fn
    return add(provider) if provider is not None else add


def _num(value: Any) -> Optional[float]:
    if value is None or isinstance(value, bool):
        return None
    try:
        out = float(value)
    except (TypeError, ValueError):
        return None
    return out if out == out else None  # NaN


def timer(source: str, kind: str, id: str, label: str, due: Any, *,
          since: Any = None, state: str = STATE_WAITING, detail: str = "",
          scope: Optional[str] = None, link: Optional[str] = None) -> Optional[dict]:
    """One timer, or ``None`` when ``due`` is not a time (a provider passes
    what it read; a record without a due time is not a countdown)."""
    due_ts = _num(due)
    if due_ts is None:
        return None
    if state not in STATES:
        raise ValueError(f"timer state {state!r} is not one of {STATES}")
    out = {"source": source, "kind": kind, "id": str(id), "label": str(label or kind),
           "due": due_ts, "since": _num(since), "state": state, "detail": str(detail or "")}
    if scope:
        out["scope"] = scope
    if link:
        out["link"] = link
    return out


def collect(session: str, cwd: str, *, now: Optional[float] = None,
            app: Optional[Mapping[str, Any]] = None,
            providers: Optional[Mapping[str, Provider]] = None) -> dict:
    """``GET /api/sessions/<name>/timers``: every provider's timers for the
    session, soonest first. A provider that raises is reported under
    ``errors`` and the others still answer."""
    ctx = Context(session=session, cwd=cwd or "",
                  now=time.time() if now is None else float(now), app=app or {})
    timers: List[dict] = []
    errors: List[dict] = []
    for source, provider in (PROVIDERS if providers is None else providers).items():
        try:
            got = provider(ctx) or []
        except Exception as exc:  # noqa: BLE001 - one source failing hides only itself
            log.exception("session timers: provider %s failed for %s", source, session)
            errors.append({"source": source, "error": str(exc) or type(exc).__name__})
            continue
        timers.extend(t for t in got if t)
    timers.sort(key=lambda t: (t["due"], t["source"], t["id"]))
    return {"session": session, "now": ctx.now, "timers": timers, "errors": errors}


@register("cflow2")
def cflow2_provider(ctx: Context) -> List[dict]:
    """The v2 runs this session drives: its main run (scope = session name)
    and its side runs (``<session>+<side>``)."""
    if not ctx.cwd:
        return []
    from . import cflow2_web

    return cflow2_web.session_timers(ctx.cwd, ctx.session)


@register("agent")
def agent_provider(ctx: Context) -> List[dict]:
    """The report timers the session's agent set for itself
    (``daemon/agent_timers.py``, the ``timer`` MCP tool). They belong to the
    session rather than to a run or a directory, so they are read by name
    alone -- a session with no cwd still has its timers."""
    from . import agent_timers

    return agent_timers.strip_timers(ctx.session)
