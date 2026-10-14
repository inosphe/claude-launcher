"""One answer per read, however many dashboards ask for it (claunch-jkvpv).

Every open dashboard polls the same few paths every five seconds, and every
poll used to run its route from scratch: two control sockets meant the queues
view and the session list were built twice per tick, four meant four times.
Measured on the live daemon before this module: of the 120 event-loop samples
under the control socket's reads, ``h_beads_queues`` held about 53 and
``h_sessions_list`` about 21.

A route wrapped with :func:`shared` answers from one place instead:

- **Joined.** A read that arrives while the same read is being computed waits
  for that computation instead of starting another.
- **Reused.** A finished answer younger than ``max_age`` seconds is served
  again, as the bytes the route wrote -- encoding is paid once as well.
- **Invalidated.** A write this daemon accepted that changes what the route
  shows drops its stored answers at once (:data:`INVALIDATES`), so what a
  person just did on the page shows on the next poll. Changes made outside
  this daemon (an agent's ``br`` call, a transcript growing, a run's state
  committed by its own session) have no event here and are bounded by
  ``max_age`` alone -- that bound is the documented staleness.

What may be wrapped: a route whose answer depends on its path, its query and
the daemon's own state, and on nothing about who is asking. The answer is
handed to every caller of the same key; a route that read the credential,
the peer or a cookie would leak one caller's answer to another.

``max_age`` 0 turns all of it off: every read runs the route, as before. That
is :func:`daemon.api.build_app`'s default, so an app built without the
daemon's configuration (every test) behaves as it always did.
"""

from __future__ import annotations

import asyncio
import collections
import functools
import time
from dataclasses import dataclass, field
from typing import Callable, Dict, FrozenSet, Optional, Tuple

from aiohttp import web

#: How long a finished answer is served again, in seconds -- the daemon
#: setting ``shared_read_max_age``. Under the dashboard's five-second poll, so
#: one tab sees an answer at most one poll older than it would have.
DEFAULT_MAX_AGE = 4.0

#: Answers kept at once (one per path + query). The wrapped routes are
#: read in a handful of query forms; the bound only matters to a client that
#: makes up queries.
KEEP = 32

#: Methods that do not change anything, and so never invalidate.
READ_METHODS = frozenset({"GET", "HEAD", "OPTIONS"})

# The session writes that come with typing and resizing: one per keystroke or
# drag, and none of them changes a row of the session list. Written as
# suffixes: a whole "/keys" route string in a module reads, to
# tests/test_delivery_contract.py, as a sender posting to the raw passthrough.
_TYPING = frozenset(
    "/api/sessions/{name}/" + suffix
    for suffix in ("keys", "resize", "paste-image", "line-sync")
)

# The session writes that change which lanes the queues view has, or whose
# sessions they belong to.
_SESSION_LIFECYCLE = frozenset({
    "/api/sessions",
    "/api/sessions/kill",
    "/api/sessions/pause",
    "/api/sessions/resume",
    "/api/sessions/respawn",
    "/api/sessions/archive",
    "/api/sessions/{name}",
    "/api/sessions/{name}/kill",
    "/api/sessions/{name}/pause",
    "/api/sessions/{name}/archive",
    "/api/sessions/{name}/respawn",
    "/api/sessions/{name}/children",
    "/api/sessions/{name}/children/{child}/kill",
    "/api/sessions/{name}/parent",
    "/api/sessions/{name}/detach",
    "/api/sessions/{name}/handoff",
    "/api/sessions/{name}/quick-fork",
    "/api/sessions/{name}/beads",
})


@dataclass(frozen=True)
class Invalidation:
    """Which accepted writes drop a route's stored answers.

    Matched against the write's route template (``/api/sessions/{name}/kill``,
    not the concrete path), so the rule names routes the way the router
    registers them -- see the test that checks every name here is one.
    """

    prefixes: Tuple[str, ...] = ()
    exact: FrozenSet[str] = frozenset()
    excluded: FrozenSet[str] = frozenset()

    def matches(self, template: str) -> bool:
        if template in self.excluded:
            return False
        if template in self.exact:
            return True
        return any(template == p or template.startswith(p + "/") for p in self.prefixes)


#: Route name -> the writes that invalidate it. Writes not named here (mesh
#: messages, the measurement window, peer traffic, ``/api/batch`` -- the
#: page's read fallback, which is a POST) leave stored answers alone; the
#: live count that set this list is on claunch-jkvpv (DESIGN AMENDMENT).
INVALIDATES: Dict[str, Invalidation] = {
    "sessions": Invalidation(prefixes=("/api/sessions",), excluded=_TYPING),
    "beads-queues": Invalidation(
        prefixes=("/api/beads", "/api/cflow", "/api/cflow2"),
        exact=_SESSION_LIFECYCLE,
        excluded=frozenset({"/api/cflow/definition/validate"}),
    ),
    # The profile listing reads config and probes every harness. Measured on
    # the live daemon: 36.5% of the event loop's samples over fifteen seconds
    # were under it, and a thread pane waiting on the same loop took seconds
    # to open (claunch-8utpv). The page's own writes to what it shows go
    # through these routes; an edit of the config file outside this daemon
    # is bounded by ``max_age``.
    "profiles": Invalidation(
        prefixes=("/api/profiles", "/api/harnesses", "/api/install"),
    ),
}


@dataclass
class _Stored:
    generation: int
    done_at: float
    status: int
    headers: Tuple[Tuple[str, str], ...]
    body: bytes
    compress: bool


@dataclass
class _Counters:
    computes: int = 0
    hits: int = 0
    joins: int = 0
    invalidations: int = 0
    by: collections.Counter = field(default_factory=collections.Counter)


class SharedReads:
    """The stored answers, the reads in flight, and their counters."""

    def __init__(self, max_age: float = 0.0, *, keep: int = KEEP,
                 clock: Callable[[], float] = time.monotonic,
                 rules: Optional[Dict[str, Invalidation]] = None) -> None:
        self.max_age = max(0.0, float(max_age or 0.0))
        self.keep = keep
        self.clock = clock
        self.rules = INVALIDATES if rules is None else rules
        self.started_at = clock()
        self._stored: "collections.OrderedDict[tuple, _Stored]" = collections.OrderedDict()
        self._flight: Dict[tuple, Tuple[int, asyncio.Future]] = {}
        self._generation: Dict[str, int] = collections.defaultdict(int)
        self._counters: Dict[str, _Counters] = collections.defaultdict(_Counters)

    @property
    def enabled(self) -> bool:
        return self.max_age > 0

    @staticmethod
    def key(name: str, request: web.Request) -> tuple:
        # Sorted, so ``?a=1&b=2`` and ``?b=2&a=1`` are one read.
        return (name, request.path, tuple(sorted(request.query.items())))

    async def read(self, name: str, request: web.Request, handler) -> web.StreamResponse:
        key = self.key(name, request)
        counters = self._counters[name]
        generation = self._generation[name]
        stored = self._stored.get(key)
        if (stored is not None and stored.generation == generation
                and self.clock() - stored.done_at <= self.max_age):
            counters.hits += 1
            self._stored.move_to_end(key)
            return _rebuild(stored)
        flight = self._flight.get(key)
        if flight is not None and flight[0] == generation:
            counters.joins += 1
        else:
            counters.computes += 1
            # A task of its own, so the reader that started it going away
            # (a closed tab, a cancelled read) does not cancel it for the
            # others waiting on it.
            task = asyncio.ensure_future(self._compute(name, key, generation, request, handler))
            # Retrieved here as well, so a failure nobody is left waiting for
            # is not reported as never retrieved.
            task.add_done_callback(lambda t: t.cancelled() or t.exception())
            flight = (generation, task)
            self._flight[key] = flight
        stored = await asyncio.shield(flight[1])
        return _rebuild(stored)

    async def _compute(self, name, key, generation, request, handler) -> _Stored:
        try:
            response = await handler(request)
            stored = _capture(response, generation, self.clock())
        finally:
            current = self._flight.get(key)
            if current is not None and current[0] == generation:
                del self._flight[key]
        # Kept only when nothing it depends on was written while it was being
        # built, and only a success: an error is handed to the reads that
        # waited on it and asked for again by the next one.
        if stored.status == 200 and self._generation[name] == generation:
            self._stored[key] = stored
            self._stored.move_to_end(key)
            while len(self._stored) > self.keep:
                self._stored.popitem(last=False)
        return stored

    def wrote(self, method: str, template: Optional[str], status: int) -> None:
        """An accepted request: drop the answers it may have changed."""
        if method in READ_METHODS or not template or status >= 400:
            return
        for name, rule in self.rules.items():
            if not rule.matches(template):
                continue
            self._generation[name] += 1
            counters = self._counters[name]
            counters.invalidations += 1
            counters.by[template] += 1
            for key in [k for k in self._stored if k[0] == name]:
                del self._stored[key]

    def stats(self) -> dict:
        now = self.clock()
        routes = {}
        for name in sorted(set(self.rules) | set(self._counters)):
            c = self._counters[name]
            kept = [s for k, s in self._stored.items() if k[0] == name]
            routes[name] = {
                "computes": c.computes,
                "hits": c.hits,
                "joins": c.joins,
                "invalidations": c.invalidations,
                "invalidated_by": dict(c.by.most_common()),
                "stored": len(kept),
                "stored_bytes": sum(len(s.body) for s in kept),
                "oldest_age_s": round(max((now - s.done_at for s in kept), default=0.0), 3),
            }
        return {
            "max_age": self.max_age,
            "enabled": self.enabled,
            "since_s": round(now - self.started_at, 3),
            "routes": routes,
        }


def _capture(response, generation: int, done_at: float) -> _Stored:
    body = getattr(response, "body", None)
    if body is None:
        body = b""
    elif not isinstance(body, (bytes, bytearray)):
        body = bytes(body)
    headers = tuple(
        (k, v) for k, v in getattr(response, "headers", {}).items()
        if k.lower() != "content-length"
    )
    return _Stored(
        generation=generation, done_at=done_at,
        status=getattr(response, "status", 200), headers=headers, body=bytes(body),
        compress=bool(getattr(response, "compression", False)),
    )


def _rebuild(stored: _Stored) -> web.Response:
    # A response object answers one request, so every reader gets its own,
    # made from the same bytes.
    resp = web.Response(body=stored.body, status=stored.status, headers=list(stored.headers))
    if stored.compress:
        resp.enable_compression()
    return resp


def shared(name: str):
    """Serve the wrapped route through the app's :class:`SharedReads`.

    ``name`` is the route's entry in :data:`INVALIDATES`. Without a
    ``shared_reads`` on the app, or with it off, the route runs as written.
    """

    def wrap(handler):
        @functools.wraps(handler)
        async def wrapped(request: web.Request):
            # A route called straight, with a request that has no app (the
            # profile route's own tests do this), runs as written too.
            app = getattr(request, "app", None)
            reads = app.get("shared_reads") if app is not None else None
            if reads is None or not reads.enabled:
                return await handler(request)
            return await reads.read(name, request, handler)

        wrapped.shared_read = name
        return wrapped

    return wrap


def route_template(request: web.Request) -> Optional[str]:
    """The registered template of the route that served ``request``."""
    try:
        resource = request.match_info.route.resource
    except AttributeError:
        return None
    return getattr(resource, "canonical", None) if resource is not None else None


def build_middleware():
    """Tell the app's :class:`SharedReads` about every write it accepted."""

    @web.middleware
    async def shared_reads_middleware(request: web.Request, handler):
        response = await handler(request)
        reads = request.app.get("shared_reads")
        if reads is not None and request.method not in READ_METHODS:
            reads.wrote(request.method, route_template(request),
                        getattr(response, "status", 200))
        return response

    return shared_reads_middleware
