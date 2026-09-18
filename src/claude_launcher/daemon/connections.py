"""What the daemon is holding open, as readable state.

The terminal is the one part of this product that lives on a socket, and
until now the daemon said nothing about those sockets while they were up:
``daemon.log`` got one line per socket *close* and nothing else. That is the
wrong end of the event to have written down. A viewer that cannot connect
leaves no close line at all -- an upgrade refused by the auth middleware
never reaches the handler, and a socket that dies with the daemon's own
process is never logged by it -- so the log reads as "nothing is wrong"
exactly when something is (claunch-restart-disconnect-banner-12p2).

So this module keeps the other half: every socket that is open right now,
with who opened it, from where, and when; plus the last few that closed,
with the code and error they closed on. Both halves are served by
``GET /api/connections`` and printed by ``claunch connections``.

Two properties this is built for, because they are what the open question
needs:

* **Per-peer counts.** A browser will only hold so many connections to one
  server at a time, and a page that keeps a socket per session terminal
  spends them quickly. Whether that ceiling is being reached is a question
  about a number the daemon can see and nobody was counting.
* **Closures with their reason.** ``code`` and ``error`` together separate a
  page that navigated away (1000/1001) from a heartbeat timeout
  (1006 with a ``TimeoutError``) from a process that vanished (no record at
  all, because nothing ran to write one).

The registry is runtime-only and dies with the daemon, like the login
cookies and the measurement window. It is a diagnosis surface, not a
history: ``CLOSED_KEEP`` bounds the closed half so an idle daemon cannot
grow it without limit.
"""

from __future__ import annotations

import itertools
import time
from datetime import datetime, timezone
from typing import Any, Optional

from aiohttp import web

#: How many closed sockets to keep. Enough to cover a restart's worth of
#: viewers reconnecting, small enough to stay a fixed cost.
CLOSED_KEEP = 100

#: How many refused requests to keep. The same bound, for the same reason.
REFUSED_KEEP = 100

_ids = itertools.count(1)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


class Registry:
    """Open sockets, and the last :data:`CLOSED_KEEP` that closed."""

    def __init__(self) -> None:
        self._open: dict[int, dict[str, Any]] = {}
        self._closed: list[dict[str, Any]] = []
        self._refused: list[dict[str, Any]] = []

    # -- writing -------------------------------------------------------- #
    def opened(self, kind: str, name: str, request: web.Request) -> dict[str, Any]:
        """Record a socket that has just been accepted.

        The returned record is the handle its own handler passes back to
        :meth:`closed`, so neither side needs to look anything up.
        """
        peer_ip, peer_port = _peer(request)
        record: dict[str, Any] = {
            "id": next(_ids),
            "kind": kind,
            "session": name,
            "peer_ip": peer_ip,
            "peer_port": peer_port,
            "user_agent": request.headers.get("User-Agent", ""),
            "origin": request.headers.get("Origin", ""),
            "query": dict(request.query),
            "opened_at": _now_iso(),
            "_mono": time.monotonic(),
        }
        self._open[record["id"]] = record
        return record

    def closed(
        self, record: dict[str, Any], code: Optional[int], error: Optional[BaseException]
    ) -> None:
        """Move a record to the closed half, with why it ended."""
        self._open.pop(record["id"], None)
        done = dict(record)
        done["closed_at"] = _now_iso()
        done["age_s"] = round(time.monotonic() - record["_mono"], 1)
        done["code"] = code
        done["error"] = repr(error) if error is not None else None
        done.pop("_mono", None)
        self._closed.append(done)
        del self._closed[:-CLOSED_KEEP]

    def refused(self, request: web.Request, reason: str) -> None:
        """Record a request the auth middleware turned away.

        This is the half that was missing entirely. A WebSocket upgrade
        refused with 401 never reaches a handler, the access log is off, and
        the browser reports it to the page as a close with no status -- so a
        terminal that will not come up looked identical, from every record
        the daemon kept, to a terminal nobody had asked for.

        The credential itself is never written down. What the record holds is
        whether one was *presented*, which is the distinction a reader needs:
        a handshake arriving with no cookie at all says the browser withheld
        it, and one arriving with a cookie the daemon does not know says the
        daemon was restarted under it.
        """
        peer_ip, peer_port = _peer(request)
        upgrade = request.headers.get("Upgrade", "").lower() == "websocket"
        self._refused.append({
            "at": _now_iso(),
            "path": request.path,
            "query": dict(request.query),
            "method": request.method,
            "upgrade": upgrade,
            "peer_ip": peer_ip,
            "peer_port": peer_port,
            "user_agent": request.headers.get("User-Agent", ""),
            "origin": request.headers.get("Origin", ""),
            "had_cookie": bool(request.cookies.get(reason_cookie_name())),
            "had_bearer": request.headers.get("Authorization", "").startswith("Bearer "),
            "reason": reason,
        })
        del self._refused[:-REFUSED_KEEP]

    # -- reading -------------------------------------------------------- #
    def open_count(self) -> int:
        """How many sockets are open, for the log lines that say so."""
        return len(self._open)

    def snapshot(self, http_connections: Optional[int] = None) -> dict[str, Any]:
        """Everything the command and the endpoint print.

        ``http_connections`` is the aiohttp server's own count of live
        connections when the caller can reach it: the sockets below are the
        WebSockets alone, and the difference between the two numbers is the
        ordinary request traffic sharing the same per-peer budget.
        """
        now = time.monotonic()
        rows = []
        for record in sorted(self._open.values(), key=lambda r: r["id"]):
            row = dict(record)
            row["age_s"] = round(now - row.pop("_mono"), 1)
            rows.append(row)
        by_peer: dict[str, int] = {}
        by_session: dict[str, int] = {}
        for row in rows:
            by_peer[row["peer_ip"]] = by_peer.get(row["peer_ip"], 0) + 1
            by_session[row["session"]] = by_session.get(row["session"], 0) + 1
        return {
            "now": _now_iso(),
            "open": rows,
            "open_count": len(rows),
            "by_peer": by_peer,
            "by_session": by_session,
            "http_connections": http_connections,
            "closed": list(reversed(self._closed)),
            "closed_kept": CLOSED_KEEP,
            "refused": list(reversed(self._refused)),
            "refused_kept": REFUSED_KEEP,
            "refused_count": len(self._refused),
        }


def reason_cookie_name() -> str:
    """The login cookie's name, read late to avoid an import cycle."""
    from .api import COOKIE_NAME

    return COOKIE_NAME


def _peer(request: web.Request) -> tuple[str, Optional[int]]:
    """The remote address, as far as the transport will say.

    A request that arrived over a transport with no peer name -- the test
    client's in-memory pair, a Unix socket -- answers ``("?", None)`` rather
    than raising: the registry is diagnosis, and a missing address is worth
    less than the rest of the record, never worth failing an upgrade over.
    """
    peer = request.transport.get_extra_info("peername") if request.transport else None
    if isinstance(peer, tuple) and len(peer) >= 2:
        return str(peer[0]), int(peer[1])
    return "?", None


def install(app: web.Application) -> Registry:
    """Give ``app`` a registry (idempotent) and hand it back."""
    registry = app.get("connections")
    if registry is None:
        registry = Registry()
        app["connections"] = registry
    return registry


def http_connection_count(app: web.Application) -> Optional[int]:
    """How many connections aiohttp's server holds, when it is reachable.

    ``__main__`` stashes the running ``web.Server`` under ``http_server``
    after the runner is set up. Tests build the app without a runner, so the
    key is absent and the count is ``None`` -- a number that is not known is
    not the same as zero, and the printers say so.
    """
    server = app.get("http_server")
    connections = getattr(server, "connections", None)
    return len(connections) if connections is not None else None
