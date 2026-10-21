"""Answers the daemon keeps in memory and rebuilds on a clock.

A few answers cost far more to compute than to read. The profile registry is a
YAML document parsed once per link of every inheritance chain; the harness
registry merges that document with the packaged one on every call; a borrow
verdict evaluates two harness policies and probes credential storage. Requests
asked for them again and again -- the session detail panel polls one session
every two seconds, the borrow picker opens per session, the profile listing is
read at every boot -- so the daemon paid the same price repeatedly for values
that change on a human timescale.

This module owns *when* those values are read and nothing else.
:class:`PeriodicSnapshot` holds one value, :class:`KeyedSnapshots` one per key.
Both rebuild on a background thread every ``interval`` seconds and serve the
last good value, so a reader never waits for a rebuild and a rebuild that
raises leaves the previous value standing (the error is kept for whoever asks
next). A writer that changed the source calls ``invalidate()`` and the next
read builds again.

The daemon's profile facts (:mod:`claude_launcher.daemon.profile_facts`) are
the first user. :mod:`claude_launcher.profile_usage` solved the same shape for
subscription usage with a request-triggered cooldown, and keeps it: a usage read
is a network call a person asked for, so it cannot run on a clock. It is the
same question, though -- how long an expensive answer stays in memory -- and
these two are where the daemon's answer lives.
"""

from __future__ import annotations

import threading
import time
from typing import Callable, Dict, Generic, List, Optional, TypeVar

T = TypeVar("T")
K = TypeVar("K")


def _now() -> float:
    return time.monotonic()


class PeriodicSnapshot(Generic[T]):
    """One value, rebuilt by a background thread every ``interval`` seconds.

    ``interval`` of 0 or less turns the thread off: the value is built on the
    first read and rebuilt only when :meth:`invalidate` is called. That is what
    tests and embedders that drive their own clock want.
    """

    def __init__(
        self,
        build: Callable[[], T],
        *,
        interval: float,
        name: str = "",
    ) -> None:
        self._build = build
        self.interval = float(interval)
        self.name = name or "snapshot"
        self._lock = threading.Lock()
        # One build at a time; held across the build so two ticks do not run
        # the same expensive read twice, but never held by readers.
        self._build_lock = threading.Lock()
        self._value: Optional[T] = None
        self._built_at = 0.0
        self._error: Optional[str] = None
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- readers ---------------------------------------------------------

    def current(self) -> T:
        """The value now; builds it if nothing has been built yet.

        A reader that arrives while a rebuild is running keeps the previous
        value: the swap happens when the build returns.
        """
        with self._lock:
            value = self._value
        if value is not None:
            return value
        return self.refresh()

    def peek(self) -> Optional[T]:
        """The current value, or None before the first build."""
        with self._lock:
            return self._value

    def age(self) -> float:
        """Seconds since the current value was built (``inf`` before that)."""
        with self._lock:
            if not self._built_at:
                return float("inf")
            return max(0.0, _now() - self._built_at)

    @property
    def error(self) -> Optional[str]:
        """Why the last rebuild failed, until one succeeds."""
        with self._lock:
            return self._error

    # -- writers ---------------------------------------------------------

    def refresh(self) -> T:
        """Rebuild now and return the new value.

        Exceptions propagate to the caller; the previous value stays in place
        and the message is kept in :attr:`error` for the next reader. The
        background thread swallows them in :meth:`tick`.
        """
        with self._build_lock:
            try:
                value = self._build()
            except Exception as exc:  # noqa: BLE001 - recorded, then re-raised
                with self._lock:
                    self._error = str(exc) or exc.__class__.__name__
                raise
            with self._lock:
                self._value = value
                self._built_at = _now()
                self._error = None
            return value

    def invalidate(self) -> None:
        """Drop the value: the next read builds it again.

        Readers that arrive between the drop and the rebuild pay for it, which
        is the point -- a config write is a person waiting to see the result.
        """
        with self._lock:
            self._value = None
            self._built_at = 0.0

    def tick(self) -> None:
        """Rebuild if the interval has passed. This is the thread's body."""
        if self.age() < self.interval:
            return
        try:
            self.refresh()
        except Exception:  # noqa: BLE001 - the error is on ``error`` already
            pass

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        """Start the rebuild thread. A non-positive interval keeps it off."""
        if self.interval <= 0 or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"claunch-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.tick()


class KeyedSnapshots(Generic[K, T]):
    """One value per key, refreshed on the same clock as a snapshot.

    A key that has never been asked for has no value: building every key up
    front means building answers nobody reads. The first read of a key builds
    it (in the caller's thread -- callers that must not block the event loop
    hand this to a worker); after that the background thread keeps every known
    key fresh, so later reads are dictionary lookups.
    """

    def __init__(
        self,
        build: Callable[[K], T],
        *,
        interval: float,
        name: str = "",
    ) -> None:
        self._build = build
        self.interval = float(interval)
        self.name = name or "keyed-snapshot"
        self._lock = threading.Lock()
        self._entries: Dict[K, tuple] = {}  # key -> (value, built_at)
        self._locks: Dict[K, threading.Lock] = {}
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()

    # -- readers ---------------------------------------------------------

    def get(self, key: K) -> Optional[T]:
        """The value for ``key``, or None if it has not been built yet."""
        with self._lock:
            entry = self._entries.get(key)
        return None if entry is None else entry[0]  # type: ignore[return-value]

    def require(self, key: K) -> T:
        """The value for ``key``, building it if it is missing."""
        value = self.get(key)
        if value is not None:
            return value
        return self.refresh(key)

    def age(self, key: K) -> float:
        with self._lock:
            entry = self._entries.get(key)
        if entry is None:
            return float("inf")
        return max(0.0, _now() - entry[1])

    def keys(self) -> List[K]:
        with self._lock:
            return list(self._entries)

    # -- writers ---------------------------------------------------------

    def refresh(self, key: K) -> T:
        """Rebuild ``key`` now and return the new value."""
        lock = self._key_lock(key)
        with lock:
            value = self._build(key)
            with self._lock:
                self._entries[key] = (value, _now())
            return value

    def invalidate(self, key: Optional[K] = None) -> None:
        with self._lock:
            if key is None:
                self._entries.clear()
            else:
                self._entries.pop(key, None)

    def tick(self) -> None:
        """Rebuild every key older than the interval."""
        if self.interval <= 0:
            return
        for key in self.keys():
            if self.age(key) < self.interval:
                continue
            try:
                self.refresh(key)
            except Exception:  # noqa: BLE001 - one bad key does not stop the rest
                continue

    # -- lifecycle -------------------------------------------------------

    def start(self) -> None:
        if self.interval <= 0 or self._thread is not None:
            return
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"claunch-{self.name}", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=1.0)

    def _run(self) -> None:
        while not self._stop.wait(self.interval):
            self.tick()

    def _key_lock(self, key: K) -> threading.Lock:
        with self._lock:
            return self._locks.setdefault(key, threading.Lock())
