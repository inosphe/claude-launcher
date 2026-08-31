"""Client-side acquisition for the machine test window.

The daemon is the primary arbiter.  A sweep falls back to an OS file lock
when the daemon or the new endpoint is unavailable, so two callers using this
version still serialize during deployment.  Targeted runs keep running in
that condition because one file lock cannot represent their shared capacity;
the fallback is reported explicitly.
"""

from __future__ import annotations

import os
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator, Mapping, Optional

from . import daemon_client
from .daemon import paths

SWEEP = "sweep"
TARGETED = "targeted"
CLASSES = (SWEEP, TARGETED)

WINDOW_MODE_ENV = "CLAUNCH_WINDOW"
WINDOW_GRANT_ENV = "CLAUNCH_WINDOW_GRANT_ID"
WINDOW_CLASS_ENV = "CLAUNCH_WINDOW_CLASS"
WINDOW_WORKERS_ENV = "CLAUNCH_WINDOW_ADVISORY_N"

DEFAULT_WAIT = 1800.0
DEFAULT_SWEEP_WORKERS = 8
DEFAULT_TARGETED_WORKERS = 4


class WindowUnavailable(RuntimeError):
    """The arbiter answered but did not grant the requested window."""


@dataclass
class WindowGrant:
    cls: str
    grant_id: str
    advisory_n: int
    source: str
    wait_seconds: float
    session: Optional[str]
    _client: Optional[daemon_client.DaemonClient] = field(default=None, repr=False)
    _lock: Optional[BinaryIO] = field(default=None, repr=False)
    _owns: bool = field(default=True, repr=False)
    _released: bool = field(default=False, repr=False)

    def child_env(self, base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        """Environment that tells a nested pytest process this grant covers it."""
        env = dict(os.environ if base is None else base)
        env[WINDOW_GRANT_ENV] = self.grant_id
        env[WINDOW_CLASS_ENV] = self.cls
        env[WINDOW_WORKERS_ENV] = str(self.advisory_n)
        return env

    def install_environment(self) -> None:
        """Publish the grant to xdist workers spawned by this pytest process."""
        os.environ[WINDOW_GRANT_ENV] = self.grant_id
        os.environ[WINDOW_CLASS_ENV] = self.cls
        os.environ[WINDOW_WORKERS_ENV] = str(self.advisory_n)

    def receipt(self) -> dict:
        return {
            "class": self.cls,
            "holder": self.session or f"pid {os.getpid()}",
            "source": self.source,
            "wait_seconds": self.wait_seconds,
            "advisory_n": self.advisory_n,
        }

    def release(self) -> None:
        if self._released or not self._owns:
            return
        self._released = True
        try:
            if self._client is not None and self.grant_id:
                self._client.post(
                    "/api/window/release",
                    {"grant_id": self.grant_id},
                    timeout=5.0,
                )
            elif self._lock is not None:
                _unlock(self._lock)
        except (daemon_client.DaemonClientError, OSError) as exc:
            print(f"WARNING: test window release failed: {exc}", file=sys.stderr)
        finally:
            if self._lock is not None:
                self._lock.close()
                self._lock = None


def inherited_grant() -> Optional[WindowGrant]:
    grant_id = os.environ.get(WINDOW_GRANT_ENV)
    if not grant_id:
        return None
    cls = os.environ.get(WINDOW_CLASS_ENV, TARGETED)
    fallback = DEFAULT_SWEEP_WORKERS if cls == SWEEP else DEFAULT_TARGETED_WORKERS
    try:
        workers = max(1, int(os.environ.get(WINDOW_WORKERS_ENV, fallback)))
    except ValueError:
        workers = fallback
    return WindowGrant(
        cls=cls,
        grant_id=grant_id,
        advisory_n=workers,
        source="inherited",
        wait_seconds=0.0,
        session=os.environ.get("CLAUNCH_SESSION"),
        _owns=False,
    )


def acquire(
    cls: str,
    *,
    label: str = "",
    wait: float = DEFAULT_WAIT,
    session: Optional[str] = None,
) -> WindowGrant:
    """Acquire a daemon grant, or the documented deployment fallback."""
    if cls not in CLASSES:
        raise ValueError(f"unknown test window class {cls!r}")
    inherited = inherited_grant()
    if inherited is not None:
        if cls == SWEEP and inherited.cls != SWEEP:
            raise WindowUnavailable(
                "a targeted parent grant cannot cover an exclusive sweep"
            )
        return inherited

    owner = session or os.environ.get("CLAUNCH_SESSION")
    if os.environ.get(WINDOW_MODE_ENV, "").strip().lower() == "off":
        workers = DEFAULT_SWEEP_WORKERS if cls == SWEEP else DEFAULT_TARGETED_WORKERS
        print(
            "WARNING: CLAUNCH_WINDOW=off; this test run has no concurrency guard",
            file=sys.stderr,
        )
        return WindowGrant(cls, "disabled", workers, "disabled", 0.0, owner)

    started = time.monotonic()
    client = daemon_client.connect()
    if client is not None:
        try:
            result = client.post(
                "/api/window/acquire",
                {
                    "class": cls,
                    "session": owner,
                    "pid": os.getpid(),
                    "label": label,
                    "wait": max(0.0, wait),
                },
                timeout=max(5.0, wait + 5.0),
            )
        except daemon_client.DaemonClientError as exc:
            return _fallback(cls, owner, started, wait, reason=str(exc))
        if result.get("granted"):
            fallback = (
                DEFAULT_SWEEP_WORKERS if cls == SWEEP else DEFAULT_TARGETED_WORKERS
            )
            try:
                workers = max(1, int(result.get("advisory_n") or fallback))
            except (TypeError, ValueError):
                workers = fallback
            return WindowGrant(
                cls=cls,
                grant_id=str(result["grant_id"]),
                advisory_n=workers,
                source="daemon",
                wait_seconds=round(time.monotonic() - started, 3),
                session=owner,
                _client=client,
            )
        detail = result.get("error") or (
            "timed out" if result.get("timeout") else "not granted"
        )
        raise WindowUnavailable(f"test window {cls} {detail}")

    return _fallback(cls, owner, started, wait, reason="daemon unavailable")


def _fallback(
    cls: str,
    owner: Optional[str],
    started: float,
    wait: float,
    *,
    reason: str,
) -> WindowGrant:
    if cls == TARGETED:
        print(
            "WARNING: targeted test window unavailable; continuing without its "
            f"capacity limit ({reason})",
            file=sys.stderr,
        )
        return WindowGrant(
            cls, "targeted-fallback", DEFAULT_TARGETED_WORKERS, "fallback", 0.0, owner
        )

    lock = _lock_file(wait)
    print(
        f"WARNING: daemon test window unavailable; sweep uses OS lock ({reason})",
        file=sys.stderr,
    )
    return WindowGrant(
        cls=cls,
        grant_id="sweep-fallback",
        advisory_n=DEFAULT_SWEEP_WORKERS,
        source="fallback",
        wait_seconds=round(time.monotonic() - started, 3),
        session=owner,
        _lock=lock,
    )


def _lock_file(wait: float) -> BinaryIO:
    lock_path = paths.daemon_dir() / "test-window-fallback.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    handle = lock_path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    deadline = time.monotonic() + max(0.0, wait)
    while True:
        try:
            _try_lock(handle)
            return handle
        except OSError:
            if wait <= 0 or time.monotonic() >= deadline:
                handle.close()
                raise WindowUnavailable("sweep fallback lock was not granted")
            time.sleep(0.2)


def _try_lock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)


def _unlock(handle: BinaryIO) -> None:
    handle.seek(0)
    if os.name == "nt":
        import msvcrt

        msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


@contextmanager
def hold(cls: str, **kwargs) -> Iterator[WindowGrant]:
    grant = acquire(cls, **kwargs)
    try:
        yield grant
    finally:
        grant.release()
