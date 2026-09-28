"""Client-side acquisition for the machine test window.

The daemon is the primary arbiter.  When the daemon or the endpoint is
unavailable both classes fall back to OS file locks, so callers on this
version still respect the window during a deployment (claunch-8kald):

* a targeted run takes one of ``FALLBACK_TARGETED_SLOTS`` slot locks -- the
  shared capacity is a fixed number of files, one per slot;
* a sweep takes the sweep lock and then every slot lock, so it excludes the
  fallback targeted runs the way the daemon's sweep excludes granted ones.
  It keeps each slot it wins while it waits for the rest, so rotating
  targeted runs cannot starve it.

``CLAUNCH_WINDOW=off`` disables the guard only in an operator shell. Inside a
managed session (``$CLAUNCH_SESSION`` set) it is ignored with a warning: the
window exists because sessions cannot see each other's load, and a session
switching it off for itself is the case the window was built against.
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
#: Set only in the environment a grant owner hands its *direct* child pytest
#: (``child_env``): that pytest reports its result into the grant, and pops
#: the variable so a pytest a test spawns does not overwrite it.
WINDOW_REPORT_ENV = "CLAUNCH_WINDOW_REPORT"

DEFAULT_WAIT = 1800.0
DEFAULT_SWEEP_WORKERS = 8
DEFAULT_TARGETED_WORKERS = 4

#: The fallback's targeted capacity: the daemon's default targeted cap.
FALLBACK_TARGETED_SLOTS = 3


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
    _locks: list = field(default_factory=list, repr=False)
    _owns: bool = field(default=True, repr=False)
    _released: bool = field(default=False, repr=False)

    def child_env(self, base: Optional[Mapping[str, str]] = None) -> dict[str, str]:
        """Environment that tells a nested pytest process this grant covers it."""
        env = dict(os.environ if base is None else base)
        env[WINDOW_GRANT_ENV] = self.grant_id
        env[WINDOW_CLASS_ENV] = self.cls
        env[WINDOW_WORKERS_ENV] = str(self.advisory_n)
        env[WINDOW_REPORT_ENV] = "1"
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
        except (daemon_client.DaemonClientError, OSError) as exc:
            print(f"WARNING: test window release failed: {exc}", file=sys.stderr)
        finally:
            _release_locks(self._locks)
            self._locks = []


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
    workers: int = 0,
) -> WindowGrant:
    """Acquire a daemon grant, or the documented deployment fallback.

    ``workers`` is the xdist width the run wants (0 = the class ceiling); a
    run that will not use xdist should say 1, so the machine's worker budget
    is charged what the run actually spends.
    """
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
        managed = os.environ.get("CLAUNCH_SESSION")
        if managed:
            print(
                f"WARNING: CLAUNCH_WINDOW=off is ignored inside managed session "
                f"{managed!r}; only the operator's shell may run tests unguarded",
                file=sys.stderr,
            )
        else:
            width = DEFAULT_SWEEP_WORKERS if cls == SWEEP else DEFAULT_TARGETED_WORKERS
            print(
                "WARNING: CLAUNCH_WINDOW=off; this test run has no concurrency guard",
                file=sys.stderr,
            )
            return WindowGrant(cls, "disabled", width, "disabled", 0.0, owner)

    started = time.monotonic()
    client = daemon_client.connect()
    if client is not None:
        body = {
            "class": cls,
            "session": owner,
            "pid": os.getpid(),
            "label": label,
            "wait": max(0.0, wait),
        }
        if workers:
            body["workers"] = max(1, int(workers))
        try:
            result = client.post(
                "/api/window/acquire",
                body,
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
        if result.get("reason"):
            detail += f" ({result['reason']})"
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
    """The deployment fallback: OS file locks for both classes."""
    deadline = time.monotonic() + max(0.0, wait)
    if cls == TARGETED:
        locks = [_take_any_slot(deadline, wait)]
        print(
            "WARNING: daemon test window unavailable; targeted run holds fallback "
            f"slot lock {locks[0].name} ({reason})",
            file=sys.stderr,
        )
        workers = DEFAULT_TARGETED_WORKERS
        grant_id = "targeted-fallback"
    else:
        locks = [_take_lock(_lock_path("test-window-fallback.lock"), deadline, wait)]
        try:
            for slot in range(FALLBACK_TARGETED_SLOTS):
                locks.append(_take_lock(_slot_path(slot), deadline, wait))
        except WindowUnavailable:
            _release_locks(locks)
            raise
        print(
            "WARNING: daemon test window unavailable; sweep holds the OS sweep "
            f"lock and every targeted slot lock ({reason})",
            file=sys.stderr,
        )
        workers = DEFAULT_SWEEP_WORKERS
        grant_id = "sweep-fallback"
    return WindowGrant(
        cls=cls,
        grant_id=grant_id,
        advisory_n=workers,
        source="fallback",
        wait_seconds=round(time.monotonic() - started, 3),
        session=owner,
        _locks=locks,
    )


def _lock_path(name: str) -> Path:
    path = paths.daemon_dir() / name
    path.parent.mkdir(parents=True, exist_ok=True)
    return path


def _slot_path(slot: int) -> Path:
    return _lock_path(f"test-window-targeted-{slot}.lock")


def _open_lock(path: Path) -> BinaryIO:
    handle = path.open("a+b")
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        handle.write(b"\0")
        handle.flush()
    return handle


def _take_lock(path: Path, deadline: float, wait: float) -> BinaryIO:
    handle = _open_lock(path)
    while True:
        try:
            _try_lock(handle)
            return handle
        except OSError:
            if wait <= 0 or time.monotonic() >= deadline:
                handle.close()
                raise WindowUnavailable(f"fallback lock {path.name} was not granted")
            time.sleep(0.2)


def _take_any_slot(deadline: float, wait: float) -> BinaryIO:
    """One free targeted slot, polling every slot until the deadline."""
    handles = [_open_lock(_slot_path(i)) for i in range(FALLBACK_TARGETED_SLOTS)]
    try:
        while True:
            for handle in handles:
                try:
                    _try_lock(handle)
                except OSError:
                    continue
                handles.remove(handle)
                return handle
            if wait <= 0 or time.monotonic() >= deadline:
                raise WindowUnavailable(
                    f"all {FALLBACK_TARGETED_SLOTS} targeted fallback slots are held"
                )
            time.sleep(0.2)
    finally:
        for handle in handles:
            handle.close()


def _release_locks(locks: list) -> None:
    for handle in reversed(locks):
        try:
            _unlock(handle)
        except OSError:
            pass
        finally:
            handle.close()


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


def reportable(grant_id: Optional[str]) -> bool:
    """Only a daemon grant has a history line to carry a result."""
    return bool(grant_id) and grant_id != "disabled" and not grant_id.endswith("-fallback")


def report_result(grant_id: str, result: dict) -> bool:
    """Attach a finished run's result to its grant in the daemon's history.

    Best effort by design: a daemon that is down, or predates the endpoint,
    costs the history one result and costs the test run nothing.
    """
    if not reportable(grant_id):
        return False
    client = daemon_client.connect()
    if client is None:
        return False
    try:
        answer = client.post(
            "/api/window/report", {"grant_id": grant_id, "result": result}, timeout=3.0
        )
    except daemon_client.DaemonClientError:
        return False
    return bool(answer.get("reported"))


def pytest_result(exitstatus: int, stats: Mapping, collected: int, duration: float) -> dict:
    """A pytest session's end state in the window's result fields."""
    outcome = {0: "passed", 1: "failed", 2: "interrupted", 5: "no_tests"}.get(
        int(exitstatus), "error"
    )
    return {
        "outcome": outcome,
        "exit_code": int(exitstatus),
        "passed": len(stats.get("passed", ())),
        "failed": len(stats.get("failed", ())),
        "errors": len(stats.get("error", ())),
        "skipped": len(stats.get("skipped", ())),
        "collected": int(collected or 0),
        "duration": round(float(duration), 1),
    }


def _expand_tx(tx) -> list:
    """xdist's ``--tx`` list with ``N*spec`` written out, one entry per worker."""
    out = []
    for spec in tx or []:
        count, star, rest = str(spec).partition("*")
        if star and count.isdigit():
            out.extend([rest] * int(count))
        else:
            out.append(str(spec))
    return out


def requested_xdist_width(option) -> int:
    """How many xdist workers this pytest invocation would start (1 = none).

    Read after xdist's ``pytest_cmdline_main`` has turned ``-n`` into
    ``--tx`` entries, which is where ``pytest_sessionstart`` stands.
    """
    return max(1, len(_expand_tx(getattr(option, "tx", None))))


def clamp_xdist_width(option, width: int) -> Optional[tuple]:
    """Cut xdist's worker list to the granted width; returns (before, after).

    Returns None when nothing changed. Only local ``popen`` workers are cut:
    a remote spec is a deliberate layout this guard has no business editing.
    xdist's DSession creates its nodes in a ``trylast`` sessionstart, so a
    conftest sessionstart that calls this runs first (pytest-xdist 3.x).
    """
    specs = _expand_tx(getattr(option, "tx", None))
    width = max(1, int(width))
    if len(specs) <= width or not all(s.startswith("popen") for s in specs):
        return None
    option.tx = specs[:width]
    if isinstance(getattr(option, "numprocesses", None), int):
        option.numprocesses = width
    return (len(specs), width)


@contextmanager
def hold(cls: str, **kwargs) -> Iterator[WindowGrant]:
    grant = acquire(cls, **kwargs)
    try:
        yield grant
    finally:
        grant.release()
