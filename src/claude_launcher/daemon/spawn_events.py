"""A failed or downgraded session creation, as a warning a person can see.

Before this module a refused spawn reached the daemon log and nothing else:
a cflow ``session`` delegation that the API refused on every attempt fell
back to a sub-agent, and the delayed sweep's watcher spawn retried into the
same refusal every five minutes, while the only trace was a traceback in
``daemon.log`` (claunch-ull6m, claunch-k3acp). The user of this daemon does
not read that log; the person watching the session does.

Two existing surfaces are used, and nothing new is opened:

- **The durable half** is the session event history
  (:mod:`claude_launcher.daemon.session_events`). It is already the daemon's
  store for mechanical lifecycle events -- create, exit, kill, fail -- each
  with a ``kind``, a ``text``, an ``at`` and a ``details`` mapping, and it is
  read by :meth:`operators_v2.OperatorService.poll` (the dashboard's operator
  feed), by the observer's evidence and by the search index. A row here
  therefore reaches the page without a new feed, survives a daemon restart,
  and stays readable after the fact.
- **The immediate half** is a ``warn`` notice
  (:meth:`claude_launcher.daemon.session.Session.notify`), the daemon's door
  for a line meant for the person at a session rather than for the program in
  it. This is the same door :class:`~claude_launcher.daemon.manager.SessionManager`
  and :mod:`~claude_launcher.daemon.handoff` use for a warning nobody should
  have to read the log to find. It is transient by design, which is why the
  event above carries the same sentence durably.

The dashboard's session rail was the third candidate the issue named and is
left alone: it draws a row per session from that session's own state, and
what this module reports is precisely the case where the session it was asked
to create does not exist. There is no row to draw it on.

**Repeats are one report.** The same target failing for the same reason
within :data:`SAME_REASON_GAP` is counted and not recorded again; the next
report that clears the gap says how many were folded into the previous one.
That is the rule the permanent-refusal case needs: an attempt every five
minutes for hours is one situation, not a hundred.

**The reason is normalised** because the same failure is seen twice: the
route reports what it answered, and a caller that reached it over HTTP
re-reports the client's rendering of the same sentence (``POST
/api/sessions/<parent>/children: <error body>``). Folding the call prefix and
the whitespace makes both the same key, so one refusal stays one report.
"""

from __future__ import annotations

import logging
import re
import time
from typing import Any, Callable, Dict, Optional, Tuple

log = logging.getLogger(__name__)

#: The grade written into the event's details. ``session_events`` has no
#: severity column of its own, so the grade rides with the rest of the body
#: (a reader that does not know the key ignores it, like any other detail).
LEVEL = "warning"

#: The grade :mod:`claude_launcher.daemon.notice` draws for the same thing.
NOTICE_LEVEL = "warn"

#: How long the notice stays up. A spawn failure is worth reading twice.
NOTICE_TTL = 60.0

#: Seconds within which the same (target, reason) is one report.
SAME_REASON_GAP = 900.0

#: The longest reason kept: one sentence, not a traceback.
MAX_REASON = 300

#: How much of the sentence the notice shows. It is drawn over one row of a
#: terminal, and the event it announces carries the rest.
NOTICE_TEXT = 140

#: Distinct (target, reason) pairs remembered before the oldest are dropped.
#: Entries only hold a timestamp and a count, so this is a guard on a daemon
#: that runs for weeks, not a tuned size.
MAX_SEEN = 500

#: What :meth:`daemon_client.DaemonClient.request` puts in front of a server's
#: error body, so the caller's copy of a route's refusal keys with the route's.
_CALL_PREFIX = re.compile(r"^(?:GET|POST|PUT|PATCH|DELETE) /\S*: ")


def normalize(reason: Any) -> str:
    """``reason`` as one comparable sentence: whitespace folded, the HTTP
    call prefix dropped, empty replaced with what a person can still act on."""
    text = " ".join(str(reason or "").split())
    text = _CALL_PREFIX.sub("", text)
    return text[:MAX_REASON] or "세션 생성이 사유 없이 실패했다"


class Failures:
    """One warning event per failed (target, reason), repeats counted.

    ``events`` is the daemon's :class:`~claude_launcher.daemon.session_events.Events`
    store and ``session_of(name)`` answers the live record for a name (or
    ``None``): the event is filed on the session a person would look at.
    """

    def __init__(
        self,
        events,
        session_of: Callable[[str], Any],
        *,
        gap: float = SAME_REASON_GAP,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.events = events
        self.session_of = session_of
        self.gap = gap
        self.clock = clock
        #: (target, reason) -> [reported at, repeats since that report]
        self.seen: Dict[Tuple[str, str], list] = {}

    def report(
        self,
        target: str,
        *,
        kind: str = "fail",
        reason: Any = "",
        site: str = "",
        notify_on: str = "",
        **details,
    ) -> Optional[dict]:
        """Record one event for ``target``, or fold it into the last report.

        ``kind`` is ``"fail"`` (a creation that did not happen) or
        ``"fallback"`` (one that happened a way the user should know about).
        ``notify_on`` names the session that gets the notice when it is not
        ``target`` -- a fallback child has no viewer yet, while the parent
        that asked for it does.

        Returns the recorded event, or ``None`` when this failure was folded
        into an earlier report.
        """
        reason = normalize(reason)
        key = (str(target or ""), reason)
        now = self.clock()
        previous = self.seen.get(key)
        if previous is not None and now - previous[0] < self.gap:
            previous[1] += 1
            return None
        repeats = previous[1] if previous else 0
        self.seen[key] = [now, 0]
        if len(self.seen) > MAX_SEEN:
            # Oldest report first: a pair not seen for that long would report
            # again anyway.
            for stale in sorted(self.seen, key=lambda k: self.seen[k][0])[:len(self.seen) - MAX_SEEN]:
                del self.seen[stale]
        session = self._session(target)
        if session is None:
            # Nothing to file it on: the target is gone (an exited owner, a
            # name that was never registered). The log keeps the sentence;
            # a report about a session nobody can open is not worth a row.
            log.warning("spawn %s for %r was not recorded: %s", kind, target, reason)
            return None
        text = self._text(kind, reason, repeats)
        event = self.events.record(
            session, kind, text, level=LEVEL, reason=reason, site=site,
            target=str(target or ""), repeats=repeats, **details
        )
        self._notice(notify_on or target, text)
        return event

    def _session(self, name: str):
        try:
            return self.session_of(str(name or ""))
        except Exception:  # noqa: BLE001 - a lookup must not fail the report
            log.debug("could not read session %r for a spawn event", name, exc_info=True)
            return None

    def _text(self, kind: str, reason: str, repeats: int) -> str:
        what = "세션 생성 fallback" if kind == "fallback" else "세션 생성 실패"
        text = f"{what}: {reason}"
        if repeats:
            text += f" — 직전 보고 뒤 {repeats}회 더"
        return text

    def _notice(self, name: str, text: str) -> None:
        """The immediate half: a warn line over that session's terminal.

        Never raises: a notice is a courtesy to whoever is looking, and the
        failure it announces has already been recorded."""
        session = self._session(name)
        if session is None:
            return
        try:
            session.notify(text[:NOTICE_TEXT], ttl=NOTICE_TTL, level=NOTICE_LEVEL)
        except Exception:  # noqa: BLE001 - a notice must never break the manager
            log.debug("spawn notice for %r failed", name, exc_info=True)


#: The reporter modules without a manager handle call (see :func:`report`).
_installed: Optional[Failures] = None


def install(failures: Optional[Failures]) -> Optional[Failures]:
    """Set the process's reporter (``None`` clears it) and return the
    previous one, so a caller that only needed it for a moment can put the
    old one back."""
    global _installed
    previous = _installed
    _installed = failures
    return previous


def installed() -> Optional[Failures]:
    """The process's reporter, or ``None`` before the daemon installed one."""
    return _installed


def report(
    target: str,
    *,
    kind: str = "fail",
    reason: Any = "",
    site: str = "",
    notify_on: str = "",
    **details,
) -> Optional[dict]:
    """Record one event through the installed reporter.

    The modules that meet a failure without a manager handle --
    :func:`onboard.inherit_mesh`, :mod:`cflow2_exec`, :mod:`delayed_sweep` --
    come through here, so the call site stays one line and survives the
    places being edited around it. Silent (and harmless) when no reporter is
    installed: a failure must not grow a second failure out of its own
    report.
    """
    failures = _installed
    if failures is None:
        log.debug("no spawn-event reporter installed: %r %s at %s", target, reason, site)
        return None
    try:
        return failures.report(
            target, kind=kind, reason=reason, site=site, notify_on=notify_on, **details
        )
    except Exception:  # noqa: BLE001 - see the docstring
        log.exception("spawn event for %r could not be recorded", target)
        return None
