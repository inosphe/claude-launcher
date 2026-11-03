"""One long-lived SQLite connection per store, shared by every thread.

The daemon's WAL stores (threads, QA, megathread, the session registry) used
to open a fresh connection for every operation and close it straight after.
On the daemon's thread pools that meant one thread opening a store's file
while another closed its own connection to the same file, many times a
second. SQLite 3.51.0's unix VFS (the build the daemon's Python links) takes
two mutexes in opposite orders on those two paths: ``findReusableFd`` (open)
holds the global ``unixBigLock`` and waits for the file's inode
``pLockMutex``, while ``unixLock`` under ``sqlite3WalClose`` (close) holds
that ``pLockMutex`` and waits for ``unixBigLock``. Both threads waited
forever, and the event loop -- opening a store of its own -- queued behind
them; the daemon stopped answering anything (2026-11-04, a stack sample of
the wedged pid showed exactly these three frames).

A store that keeps one connection open for its lifetime never runs either
path after startup, so the inversion cannot form, and the event loop no
longer pays an open and a close per call. Access is serialised with a
re-entrant lock: a store's calls are short, and a nested block on the same
thread (a helper that reads inside a write) reuses the connection it is
already holding.
"""

from __future__ import annotations

import itertools
import sqlite3
import threading
import weakref
from contextlib import contextmanager
from pathlib import Path
from typing import Iterable, Iterator, Optional, Union

#: The pragmas every WAL store set on each fresh connection.
WAL_PRAGMAS = (
    "PRAGMA journal_mode=WAL",
    "PRAGMA synchronous=NORMAL",
    "PRAGMA busy_timeout=30000",
)


class SharedConnection:
    """A connection opened once and handed to one thread at a time.

    :meth:`use` yields the connection under the lock; :meth:`write` wraps the
    block in ``BEGIN IMMEDIATE`` ... ``COMMIT`` (``ROLLBACK`` on an
    exception), or in a savepoint when the thread is already inside a
    transaction on it. A block that leaves a transaction open when the
    outermost :meth:`use` ends is rolled back, which is what closing a
    per-operation connection used to do, so it never leaks into the next
    caller's statements.
    """

    def __init__(
        self,
        path: Union[str, Path],
        *,
        timeout: float = 30.0,
        isolation_level: Optional[str] = None,
        row_factory=None,
        pragmas: Iterable[str] = WAL_PRAGMAS,
    ) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._depth = 0
        self._savepoints = itertools.count(1)
        conn = sqlite3.connect(
            self.path,
            timeout=timeout,
            isolation_level=isolation_level,
            check_same_thread=False,
        )
        try:
            if row_factory is not None:
                conn.row_factory = row_factory
            for pragma in pragmas:
                conn.execute(pragma)
        except BaseException:
            conn.close()
            raise
        self._conn: Optional[sqlite3.Connection] = conn
        # A store dropped without close() (tests, a CLI command) still
        # closes its connection, instead of leaving it to interpreter exit.
        self._finalizer = weakref.finalize(self, conn.close)

    @contextmanager
    def use(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            conn = self._conn
            if conn is None:
                raise sqlite3.ProgrammingError(f"{self.path}: the shared connection is closed")
            self._depth += 1
            try:
                yield conn
            finally:
                self._depth -= 1
                if self._depth == 0 and conn.in_transaction:
                    conn.rollback()

    @contextmanager
    def write(self) -> Iterator[sqlite3.Connection]:
        with self.use() as conn:
            if conn.in_transaction:
                name = f"nested_{next(self._savepoints)}"
                conn.execute(f"SAVEPOINT {name}")
                try:
                    yield conn
                except BaseException:
                    if conn.in_transaction:
                        conn.execute(f"ROLLBACK TO {name}")
                        conn.execute(f"RELEASE {name}")
                    raise
                conn.execute(f"RELEASE {name}")
                return
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                if conn.in_transaction:
                    conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")

    def close(self) -> None:
        with self._lock:
            self._conn = None
            self._finalizer()
