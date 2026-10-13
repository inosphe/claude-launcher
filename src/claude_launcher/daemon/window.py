"""The measurement window: one machine, one arbiter, two classes.

What this replaces, and why it had to be replaced, is on the board
(``claunch-8y5j``, 2026-08-29): the sweep-window protocol stood on process
scans and mesh chat, and four holes were measured -- a scan blind spot of one
to three seconds at startup (``claunch-rwq``, round 12: an entrant 46s after
the holder's start, 33s of overlap), a stale holder blocking the whole queue
until a human noticed (s286's slot 6b), a protocol that never reaches sessions
outside the mesh (``claunch-fnhu``, round 13), and a queue that lived in the
leader's messages. The leader's ruling on ``claunch-rwq`` fixed the direction
-- an occupied/released *state*, not declarations -- and this module is the
state.

The arbiter is the daemon because of what the daemon already is: a single
process (so two acquisitions racing are serialised by construction, and the
scan blind spot stops existing), always up while any session is (every session
is daemon-managed), and the one component that *knows* when a session ends --
:meth:`WindowManager.session_exited` rides the same ``manager.exit_hooks``
funnel as the board's sweep (``daemon/beads.py``), so a holder that dies
releases itself instead of waiting to be noticed.

Two classes, because the resource has two shapes (operator-set values,
2026-08-29, board ``claunch-8y5j``; the measured revision belongs to
``claunch-bl0e``):

* ``sweep`` -- a full-suite run. Capacity 1 and **exclusive against
  everything**: while a sweep holds, no targeted run is granted, and once a
  sweep is *queued* no new targeted grant is made (writer preference -- under
  rotating targeted runs an exclusive requester would starve). Exclusivity is
  the measured requirement, not caution: a targeted run overlapping a full
  sweep flipped red to green in 78 seconds on an identical tree (the
  ``claunch-1dre`` table, 2026-08-27), so a grant that admitted the pair would
  be minting measurements nobody may cite.
* ``targeted`` -- a nodeid-selected run. Capacity 3 (5 until claunch-8kald),
  shared. The cap is enforced by *not granting*, which is what
  ``claunch-95fa``'s third fix asked for: the per-session scan becomes the
  arbiter's knowledge, and eight concurrent targeted runs (where s155/s148
  met xdist node-down) cannot assemble.

The caps are deliberately NOT derived from the CPU count: this suite is not
CPU-bound (32 cores at 15% under 22 pytest processes -- s159's measurement in
``claunch-95fa``); its cost axes are process spawn and PTY/daemon waits. What
the CPU count does govern is the width of *one* run, so each grant carries
``advisory_n`` -- cores divided by active runs, clamped -- which is the fair
scheduling the arbiter is the only component positioned to do, because the
arbiter is the one that knows how many runs are active.

Since claunch-8kald (2026-09-28, user-direct) the width is also *spent*, not
only advised. Three limits bind a grant besides the caps, all read live from
the daemon config:

* a per-class width ceiling (``window_sweep_width`` 8,
  ``window_targeted_width`` 4) -- a targeted run selects a few modules and
  gains nothing from eight workers;
* a machine worker budget (``window_worker_budget`` 12): each holder records
  the width it was granted, and a targeted request waits while the budget is
  spent. The requester states the width it wants (``workers``), so a
  one-module run costs one worker, not four;
* one targeted grant per session at a time (``window_targeted_per_session``).
  A session's subagents run under the same ``CLAUNCH_SESSION``, and parallel
  subagent pytest runs were the uncounted load behind several flaky rounds.

``tests/conftest.py`` clamps the xdist worker count to the granted width, so
a ``-n 16`` typed by hand spends what the arbiter granted and no more.

Run history (claunch-8kald): every grant that ends -- released, reaped, or
ended with its session -- and every request that gave up waiting leaves one
line in ``window-history.jsonl`` next to the state file: class, holder,
label, width, the wait and the hold in seconds, and how it ended. The daemon
records the timing itself, from its own grant state; the *result* (passed /
failed counts, exit status) comes from ``tests/conftest.py``, which reports
it to the window API when pytest finishes, so no session has to remember to.
**The log keeps at most 7 days** (``HISTORY_RETENTION_DAYS``): older lines
are dropped on load and whenever the file is appended to. It is an operating
record for "how long do runs wait and hold, and how often do they fail", not
an archive; the evidence a round cites stays in its receipts.

One window per machine, not per daemon (claunch-afmzw, 2026-10-13): a named
instance (``CLAUNCH_DAEMON=kimyori``) is a second daemon process on the same
machine, and while each instance kept its own ``window.json`` the two worker
budgets of 12 were spent side by side without either seeing the other. So
every instance under one base home (:func:`paths.base_home`) reads and writes
one state file -- the default instance's ``<base home>/daemon/window.json`` --
and every operation reloads it under an OS file lock (``window.lock``) before
deciding anything. The arbiter is still "one decision at a time"; the lock is
what makes that true across processes. What only the owning daemon can judge
stays with it: each entry names its ``instance``, a session's liveness is
judged by the instance that runs it, a foreign *waiter* is dropped when the
daemon process holding its long-poll (``daemon_pid``) is gone, a foreign
*bound* holder is reaped by its pid as before (same machine, same pid space),
and a foreign session-held grant is reaped once its daemon is gone and no
daemon serves that instance any more (claunch-afmzw.3).
``window_scope: instance`` in the daemon config restores the per-instance file.

The worker budget adapts (claunch-afmzw): measured on 2026-10-1x, a run whose
lifetime overlapped 0-3 other workers failed 19% of the time and one that
overlapped 16-19 failed 68%. A run that reports test failures classified as
timeouts (``result.timeouts``, counted by ``test_window.pytest_result``) halves
the effective budget, down to ``window_adaptive_floor``; each passed run adds
one back until the configured budget is reached again (AIMD, as in TCP
congestion control). Like TCP's one decrease per window, a run granted before
the last decrease does not decrease it again -- it ran under the old load.
``window_adaptive: 0`` turns this off.

The limits themselves are the main daemon's (claunch-k7x63, 2026-10-13): on
one machine window every instance must apply the same caps, widths and
budget, and each instance reads its own config file. So the default instance
publishes the limits it enforces into the shared state (``limits``), and an
instance on a shared window enforces what the main one published, not its
own ``window_*`` keys. An operator who changes a limit on the Window page
changes it on the main daemon only (:meth:`WindowManager.set_limits`); the
change is kept in the shared state as an override of the main daemon's config
-- machine-local, so ``~/.claunch.yaml``, which is synced between machines,
does not carry one machine's core count to another. The config keys stay the
defaults an override replaces; clearing an override returns to them.

What is deliberately NOT here:

* a wall-clock TTL. The repository has already paid for that confusion once
  (s143's 19 minutes, cut by a tool ceiling and lost silently): a clock cannot
  tell a stuck holder from a slow legitimate run, and the honest reapers --
  the session exit hook and pid liveness -- already cover death. A holder
  whose session lives and whose pid lives is holding.
* preemption. A forced grant (below) is added next to the running holders;
  nothing running is stopped.

Operator overrides (claunch-8kald): the queue is FIFO with writer preference
until the operator says otherwise. ``prioritize`` gives a waiting request a
priority (higher first, FIFO within a priority; the default is 0). Requests
can also lower their own priority at acquisition: a background sweep at -1
does not block new default-priority targeted requests while it waits. Once
granted it remains exclusive and is not preempted. Positive acquisition
priority remains an operator action. ``force``
grants a waiting request -- or a fresh operator acquisition --
immediately, past every cap, budget and exclusivity rule. A forced holder
still counts against the caps and the budget for everyone after it. Both are
operator actions: the CLI refuses them inside a managed session (the
``claunch daemon restart`` gate's rule), and the API refuses to force a grant
that names a session.
* enforcement against a process that never asks. That half lives at the point
  of consumption: ``tools/sweep.py run`` acquires before it runs, and
  ``tests/conftest.py`` holds the window for any full-suite pytest however
  launched. This module is the state those two consult.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import json
import logging
import os
import secrets
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Tuple

from .. import atomic, store
from . import paths

log = logging.getLogger(__name__)

#: The two resource classes. See the module docstring for why sweep is
#: exclusive and targeted is not, and why the caps are not CPU-derived.
SWEEP = "sweep"
TARGETED = "targeted"
CLASSES = (SWEEP, TARGETED)

#: ``advisory_n`` bounds: a run narrower than 2 pays xdist's fixed costs for
#: nothing; wider than 8 buys nothing this suite can spend (the bottleneck is
#: spawn and PTY/daemon waits, not cores).
ADVISORY_MIN = 2
ADVISORY_MAX = 8

#: The spending limits (see the module docstring), used when no daemon config
#: is consulted -- tests that inject ``caps`` get these, not the machine's.
DEFAULT_LIMITS = {
    "sweep_width": 8,
    "targeted_width": 4,
    "worker_budget": 12,
    "targeted_per_session": 1,
    # AIMD on the worker budget (claunch-afmzw): 1 = on, 0 = off; the floor
    # is the lowest budget a run of timeouts can drive it to.
    "adaptive": 1,
    "adaptive_floor": 4,
}

#: Every limit the Window page and ``PUT /api/window/limits`` may set, with
#: the range a value must fall in (claunch-k7x63). ``worker_budget`` 0 turns
#: the budget off and ``targeted_per_session`` 0 removes the per-session limit,
#: as in the config keys they override.
LIMIT_BOUNDS = {
    "sweep_cap": (1, 8),
    "targeted_cap": (1, 64),
    "sweep_width": (1, 512),
    "targeted_width": (1, 512),
    "worker_budget": (0, 4096),
    "targeted_per_session": (0, 64),
    "adaptive": (0, 1),
    "adaptive_floor": (1, 4096),
}

#: The caps' config defaults, beside ``DEFAULT_LIMITS``.
DEFAULT_CAPS = {"sweep_cap": 1, "targeted_cap": 3}


class LimitsNotEditable(Exception):
    """The limits are set on another daemon (the main one)."""


#: How often a queued request re-reads the shared state: a grant made by
#: another daemon process (or a release there) sets no event in this one.
SHARED_POLL = 1.0
#: How long an operation waits for the cross-process state lock before it
#: proceeds on its own view (logged). Operations hold the lock for one
#: read-modify-write of a small JSON file, so this is a stuck peer, not load.
LOCK_WAIT = 10.0
#: The same wait on the thread that runs the daemon's event loop: there a
#: sleeping wait stops every API request and terminal of this daemon, so a
#: stuck peer costs at most this long before the operation proceeds on this
#: daemon's view (claunch-afmzw.2).
LOOP_LOCK_WAIT = 0.5

#: Server-side ceiling on an acquire's wait.  A client may choose a shorter
#: wait at assignment time, but no request may keep a daemon connection open
#: for more than 30 minutes.
MAX_WAIT = 1800.0

#: A holder reminder is deliberately independent from the session reminder
#: service.  It concerns a machine-wide resource, including sessions without
#: a cflow run, and must still reach an idle session after a failed test.
REMINDER_INTERVAL = 180.0
REMINDER_POLL = 15.0

#: How often a queued long-poll checks that its client is still connected
#: (claunch-zawnr). aiohttp does not cancel a handler whose client went away,
#: so without this a waiter killed mid-wait stayed queued, was granted later
#: with nobody to receive the grant id, and as a session's one targeted grant
#: blocked that session until someone released it by hand.
GONE_POLL = 1.0

_STATE_VERSION = 1

#: How long the run history is kept. Stated to the user in the CLI help, the
#: web Window page and the API answer; change all of them together.
HISTORY_RETENTION_DAYS = 7
HISTORY_RETENTION = HISTORY_RETENTION_DAYS * 86400.0
#: How often an append also rewrites the file without its expired lines.
HISTORY_PRUNE_EVERY = 3600.0

#: Finished runs needed before :meth:`WindowManager.estimate` offers a
#: median for a grant's length (claunch-bwzkt).
ESTIMATE_MIN_SAMPLES = 3

#: The result fields a reporter may attach to a grant, and their types.
RESULT_FIELDS = {
    "outcome": str,
    "exit_code": int,
    "passed": int,
    "failed": int,
    "errors": int,
    "skipped": int,
    "collected": int,
    "duration": float,
    # Failed/errored tests whose report reads as a timeout (claunch-afmzw).
    "timeouts": int,
}
OUTCOMES = ("passed", "failed", "error", "interrupted", "no_tests")


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _parse_ts(value) -> Optional[float]:
    try:
        return datetime.fromisoformat(str(value)).timestamp()
    except (TypeError, ValueError):
        return None


def _seconds_between(start, end) -> Optional[float]:
    a, b = _parse_ts(start), _parse_ts(end)
    if a is None or b is None:
        return None
    return round(max(0.0, b - a), 1)


def clean_result(raw) -> Optional[dict]:
    """The reported result, reduced to known fields of the right type."""
    if not isinstance(raw, dict):
        return None
    out = {}
    for key, kind in RESULT_FIELDS.items():
        if key not in raw or raw[key] is None:
            continue
        try:
            out[key] = kind(raw[key])
        except (TypeError, ValueError):
            continue
    if "outcome" in out and out["outcome"] not in OUTCOMES:
        out.pop("outcome")
    return out or None


def _quantile(values: List[float], q: float) -> Optional[float]:
    if not values:
        return None
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return round(ordered[index], 1)


def _spread(values: List[float]) -> dict:
    return {
        "p50": _quantile(values, 0.5),
        "p90": _quantile(values, 0.9),
        "max": round(max(values), 1) if values else None,
        "total": round(sum(values), 1),
    }


def pid_alive(pid: int) -> bool:
    """Is there a live process with this pid? (Best effort, no dependencies.)

    Windows has no ``kill(pid, 0)`` -- ``os.kill`` there *terminates* -- so
    the check is OpenProcess + GetExitCodeProcess. POSIX takes the signal-0
    path. A pid that cannot be queried at all (access denied) counts as
    alive: reaping on an unreadable pid would reap strangers' processes on a
    multi-user machine.
    """
    if pid <= 0:
        return False
    if sys.platform == "win32":
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        handle = kernel32.OpenProcess(0x1000, False, pid)  # QUERY_LIMITED_INFORMATION
        if not handle:
            return False
        try:
            code = ctypes.c_ulong()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return True  # handle opened; unreadable exit code is not death
            return code.value == 259  # STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
        return True
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return False


def _clean_limits(raw) -> dict:
    """The known, in-range integer limits of a stored ``limits`` block.

    The state file is shared between processes and edited by hand at worst;
    a value that does not parse or falls outside :data:`LIMIT_BOUNDS` is
    dropped, and the config default stands in for it."""
    if not isinstance(raw, dict):
        return {}
    out = {}
    for key, (low, high) in LIMIT_BOUNDS.items():
        if key not in raw:
            continue
        try:
            value = int(raw[key])
        except (TypeError, ValueError):
            continue
        if low <= value <= high:
            out[key] = value
    return out


def validate_limits(changes) -> dict:
    """Check a ``set_limits`` request: known keys, integers, in range.

    ``None`` as a value clears that override. Raises ValueError naming the
    first bad entry -- a request is applied whole or not at all."""
    if not isinstance(changes, dict) or not changes:
        raise ValueError("limits must be a non-empty object of name: value")
    out = {}
    for key, value in changes.items():
        if key not in LIMIT_BOUNDS:
            raise ValueError(
                f"unknown limit {key!r} (known: {', '.join(LIMIT_BOUNDS)})"
            )
        if value is None:
            out[key] = None
            continue
        if isinstance(value, bool):
            value = int(value)
        try:
            number = int(value)
        except (TypeError, ValueError):
            raise ValueError(f"{key} must be an integer, got {value!r}") from None
        if isinstance(value, float) and value != number:
            raise ValueError(f"{key} must be an integer, got {value!r}")
        low, high = LIMIT_BOUNDS[key]
        if not low <= number <= high:
            raise ValueError(f"{key} must be between {low} and {high}, got {number}")
        out[key] = number
    return out


def default_state_path() -> Path:
    """Where this daemon's window lives: the machine's file under the base
    home, unless the config keeps the window per instance."""
    scope = str(store.daemon_config().get("window_scope") or "machine").strip().lower()
    if scope == "instance":
        return paths.daemon_dir() / "window.json"
    return paths.base_home() / "daemon" / "window.json"


def _instance_served(instance: str) -> bool:
    """Whether a daemon process is running for ``instance`` now, by the pid
    its ``daemon.json`` records. Unreadable or absent counts as not served."""
    base = paths.base_home()
    state = base / "daemons" / instance if instance else base / "daemon"
    try:
        doc = json.loads((state / "daemon.json").read_text(encoding="utf-8"))
        pid = int(doc.get("pid") or 0)
    except (OSError, ValueError, TypeError, AttributeError):
        return False
    return pid > 0 and pid_alive(pid)


def _on_event_loop() -> bool:
    """Whether the caller runs on a thread that is running an asyncio loop."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return False
    return True


def _try_flock(handle) -> bool:
    try:
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        return True
    except OSError:
        return False


def _unflock(handle) -> None:
    with contextlib.suppress(OSError):
        if sys.platform == "win32":
            import msvcrt

            handle.seek(0)
            msvcrt.locking(handle.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(handle.fileno(), fcntl.LOCK_UN)


def _shared(method):
    """Run a public method on the machine's current state, under its lock."""

    @functools.wraps(method)
    def wrapper(self, *args, **kwargs):
        with self._locked():
            return method(self, *args, **kwargs)

    return wrapper


class WindowManager:
    """Holds and grants the measurement window; persists it across restarts.

    ``caps`` returns ``(sweep_cap, targeted_cap)`` and is read on every
    operation, so an operator's edit lands without a restart -- the pattern
    the cflow reminder keys already use. ``manager`` may be None in tests;
    session-liveness reaping then has nothing to consult.

    Holder changes are pull-based observability through :meth:`status` and
    the window API. They are expected high-frequency state transitions, so
    they do not enter the mesh delivery path, where every delivery is an
    agent-facing user message.
    """

    def __init__(
        self,
        manager=None,
        *,
        caps: Optional[Callable[[], Tuple[int, int]]] = None,
        limits: Optional[Callable[[], dict]] = None,
        state_path: Optional[Path] = None,
        cores: Optional[int] = None,
        instance: Optional[str] = None,
        owns_limits: Optional[bool] = None,
    ) -> None:
        self._manager = manager
        self._caps_src = caps or self._caps_from_config
        if limits is not None:
            self._limits_src = limits
        elif caps is not None:
            # A caller that injects its caps is a test fixing the window's
            # shape; reading the machine's config under it would make the
            # test depend on whoever runs it.
            self._limits_src = lambda: dict(DEFAULT_LIMITS)
        else:
            self._limits_src = self._limits_from_config
        self._state_path = state_path or default_state_path()
        self._cores = cores or (os.cpu_count() or 4)
        self._holders: List[dict] = []
        self._queue: List[dict] = []
        # The AIMD budget (None = the configured budget) and when it last
        # went down; shared through the state file like the holders.
        self._adaptive: dict = {"budget": None, "decreased_at": None, "reason": ""}
        self._granted_events: Dict[str, asyncio.Event] = {}
        self._history_path = self._state_path.parent / "window-history.jsonl"
        self._history: List[dict] = []
        self._history_pruned_at = 0.0
        self._history_sig: Optional[Tuple[int, int]] = None
        # Which daemon this is: entries carry it so a second daemon on the
        # same state file judges only its own sessions (claunch-afmzw).
        self._instance = paths.instance() if instance is None else instance
        self._daemon_pid = os.getpid()
        self._lock_path = self._state_path.parent / "window.lock"
        # The limits the main daemon published, and the overrides set on it
        # from the Window page (claunch-k7x63). Owning the limits: the main
        # daemon on the machine window, or every daemon on its own window.
        self._published: Optional[dict] = None
        self._overrides: dict = {}
        self._limits_meta: dict = {}
        if owns_limits is None:
            owns_limits = self._instance == "" or (
                self._state_path.parent.resolve()
                != (paths.base_home() / "daemon").resolve()
            )
        self._owns_limits = bool(owns_limits)
        self._lock_handle = None
        self._lock_depth = 0
        # A save that failed leaves memory newer than the file; reloading
        # then would roll the window back, so it is skipped until a save works.
        self._saved_ok = True
        with self._locked():
            self._drop_restored_queue()
            if self._owns_limits:
                # Publish this daemon's limits now: an instance that starts
                # before the next grant must not enforce its own config.
                self._save()

    # ---- the cross-process lock (claunch-afmzw) ------------------------- #

    @contextlib.contextmanager
    def _locked(self):
        """Hold the state lock and work on the file's current state.

        Re-entrant: the public methods nest (``release_session`` releases),
        and only the outermost entry locks and reloads.
        """
        if self._lock_depth:
            self._lock_depth += 1
            try:
                yield
            finally:
                self._lock_depth -= 1
            return
        handle = None
        try:
            self._lock_path.parent.mkdir(parents=True, exist_ok=True)
            handle = open(self._lock_path, "a+b")
            wait = LOOP_LOCK_WAIT if _on_event_loop() else LOCK_WAIT
            deadline = time.monotonic() + wait
            while not _try_flock(handle):
                if time.monotonic() >= deadline:
                    log.warning(
                        "window: state lock %s not taken in %.1fs; deciding on "
                        "this daemon's view", self._lock_path, wait,
                    )
                    handle.close()
                    handle = None
                    break
                time.sleep(0.02)
        except OSError as exc:
            log.warning("window: state lock unavailable: %s", exc)
            if handle is not None:
                handle.close()
            handle = None
        self._lock_handle = handle
        self._lock_depth = 1
        try:
            self._load()
            self._refresh_history()
            yield
        finally:
            self._lock_depth = 0
            self._lock_handle = None
            if handle is not None:
                _unflock(handle)
                handle.close()

    def _mine(self, entry: dict) -> bool:
        """Whether this daemon instance made ``entry`` (absent = the default
        instance: entries written before claunch-afmzw came from it)."""
        return (entry.get("instance") or "") == self._instance

    def _same_session(self, entry: dict, session: Optional[str]) -> bool:
        return bool(session) and entry.get("session") == session and self._mine(entry)

    # ---- configuration --------------------------------------------------- #

    @staticmethod
    def _caps_from_config() -> Tuple[int, int]:
        cfg = store.daemon_config()
        return (
            int(cfg.get("window_sweep_cap", 1)),
            int(cfg.get("window_targeted_cap", 3)),
        )

    @staticmethod
    def _limits_from_config() -> dict:
        cfg = store.daemon_config()
        return {
            key: int(cfg.get(f"window_{key}", default))
            for key, default in DEFAULT_LIMITS.items()
        }

    def _own_limits(self) -> dict:
        """The limits this daemon would enforce on its own: its config (or
        the injected values), with the Window page's overrides on top."""
        sweep_cap, targeted_cap = self._caps_src()
        merged = {"sweep_cap": int(sweep_cap), "targeted_cap": int(targeted_cap)}
        merged.update({k: int(v) for k, v in dict(self._limits_src()).items()})
        merged.update(self._overrides)
        return merged

    def _all_limits(self) -> dict:
        """The limits in force: the owner's own, or what the owner published.

        An instance that does not own the limits falls back to its own config
        only while nothing was ever published (no main daemon has run on this
        machine window yet)."""
        if self._owns_limits or not self._published:
            return self._own_limits()
        merged = self._own_limits()
        merged.update(self._published)
        return merged

    def _caps(self) -> Tuple[int, int]:
        limits = self._all_limits()
        return (
            int(limits.get("sweep_cap", DEFAULT_CAPS["sweep_cap"])),
            int(limits.get("targeted_cap", DEFAULT_CAPS["targeted_cap"])),
        )

    def _limits(self) -> dict:
        limits = self._all_limits()
        return {k: limits.get(k, v) for k, v in DEFAULT_LIMITS.items()}

    def _cap(self, cls: str) -> int:
        sweep_cap, targeted_cap = self._caps()
        return max(1, sweep_cap if cls == SWEEP else targeted_cap)

    def _class_width(self, cls: str) -> int:
        key = "sweep_width" if cls == SWEEP else "targeted_width"
        return max(1, int(self._limits().get(key, DEFAULT_LIMITS[key])))

    def _held_width(self, holder: dict) -> int:
        # A holder persisted before widths were recorded is charged its
        # class ceiling: over-counting delays a grant, under-counting
        # overloads the machine.
        workers = holder.get("workers")
        return int(workers) if workers else self._class_width(holder.get("cls", TARGETED))

    def workers_in_use(self) -> int:
        return sum(self._held_width(h) for h in self._holders)

    def _configured_budget(self) -> int:
        return int(self._limits().get("worker_budget", DEFAULT_LIMITS["worker_budget"]))

    def _adaptive_on(self) -> bool:
        return bool(int(self._limits().get("adaptive", DEFAULT_LIMITS["adaptive"]) or 0))

    def _adaptive_floor(self) -> int:
        floor = int(self._limits().get("adaptive_floor", DEFAULT_LIMITS["adaptive_floor"]) or 1)
        return max(1, min(floor, max(1, self._configured_budget())))

    def _budget(self) -> int:
        """The budget grants are decided against: the configured one, or the
        AIMD budget below it after timeouts (claunch-afmzw). 0 = off."""
        configured = self._configured_budget()
        if configured <= 0 or not self._adaptive_on():
            return configured
        current = self._adaptive.get("budget")
        if current is None:
            return configured
        return max(self._adaptive_floor(), min(configured, int(current)))

    def _budget_left(self) -> Optional[int]:
        """Workers the budget still allows; None when the budget is off."""
        budget = self._budget()
        if budget <= 0:
            return None
        return budget - self.workers_in_use()

    def _width(self, cls: str, requested: Optional[int]) -> int:
        """The xdist width a grant made now would carry."""
        width = min(self._class_width(cls), self.advisory_n(extra=1))
        if requested:
            width = min(width, max(1, int(requested)))
        left = self._budget_left()
        if left is not None:
            width = min(width, max(1, left))
        return max(1, width)

    # ---- persistence ------------------------------------------------------ #

    def _load(self) -> None:
        """Read the persisted window, if any. Reaping is deliberately NOT done
        here: on boot the sessions that held grants may still be restoring, so
        death is judged lazily at the next acquire/status instead."""
        if not self._saved_ok:
            return
        try:
            doc = json.loads(self._state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return
        if doc.get("version") != _STATE_VERSION:
            return
        self._holders = list(doc.get("holders") or [])
        self._queue = list(doc.get("queue") or [])
        limits = doc.get("limits")
        if isinstance(limits, dict):
            self._published = _clean_limits(limits.get("values")) or None
            self._overrides = _clean_limits(limits.get("overrides"))
            self._limits_meta = {
                k: limits.get(k) for k in ("by", "set_by", "set_at") if limits.get(k)
            }
        adaptive = doc.get("adaptive")
        if isinstance(adaptive, dict):
            self._adaptive = {
                "budget": adaptive.get("budget"),
                "decreased_at": adaptive.get("decreased_at"),
                "reason": adaptive.get("reason") or "",
            }

    def _drop_restored_queue(self) -> None:
        """Forget the waiters a previous daemon process left on disk.

        A queued request is a long-poll on a connection, and no connection
        outlives the process that accepted it: the client was answered with a
        connection error and moved on (``test_window`` falls back to its file
        locks). An entry restored here has nobody behind it, and granting it
        later recorded a holder no one would release (claunch-zawnr).
        Holders are kept -- a grant outlives its request by design.
        """
        mine = [q for q in self._queue if self._mine(q)]
        if not mine:
            return
        for entry in mine:
            self._record(entry, "disconnected")
        log.info("window: dropped %d waiter(s) left by the previous daemon",
                 len(mine))
        # Another instance's waiters are long-polls on a daemon that lives on.
        self._queue = [q for q in self._queue if not self._mine(q)]
        self._save()

    def _save(self) -> None:
        doc = {
            "version": _STATE_VERSION,
            "saved_at": _utcnow(),
            "holders": self._holders,
            "queue": self._queue,
            "adaptive": self._adaptive,
        }
        if self._owns_limits:
            # The owner republishes on every save, so an edit of its config
            # keys reaches the other instances with the next decision.
            self._published = self._own_limits()
        if self._published or self._overrides:
            doc["limits"] = {
                "values": self._published or {},
                "overrides": self._overrides,
                **self._limits_meta,
            }
            if self._owns_limits:
                doc["limits"]["by"] = self._instance or "default"
        try:
            self._state_path.parent.mkdir(parents=True, exist_ok=True)
            with atomic.scratch(self._state_path) as tmp:
                tmp.write_text(
                    json.dumps(doc, indent=1, ensure_ascii=False), encoding="utf-8"
                )
                atomic.replace(tmp, self._state_path)
            self._saved_ok = True
        except OSError as exc:
            # The in-memory window is the arbiter; the file is its survivor.
            # A failed save must not fail the grant it was recording.
            self._saved_ok = False
            log.warning("window: state not saved: %s", exc)

    # ---- run history (7 days) ------------------------------------------- #

    def _history_cutoff(self, now: Optional[float] = None) -> float:
        return (time.time() if now is None else now) - HISTORY_RETENTION

    def _history_signature(self) -> Optional[Tuple[int, int]]:
        try:
            st = self._history_path.stat()
        except OSError:
            return None
        return (st.st_mtime_ns, st.st_size)

    def _refresh_history(self) -> None:
        """Re-read the log when another daemon process appended to it."""
        sig = self._history_signature()
        if sig is not None and sig != self._history_sig:
            self._load_history()
            self._history_sig = self._history_signature()

    def _load_history(self) -> None:
        """Read the kept days of the log; expired lines are dropped here."""
        try:
            lines = self._history_path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return
        cutoff = self._history_cutoff()
        kept = []
        for line in lines:
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if (_parse_ts(entry.get("ended_at")) or 0) >= cutoff:
                kept.append(entry)
        self._history = kept
        if len(kept) != len(lines):
            self._rewrite_history()

    def _rewrite_history(self) -> None:
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            with atomic.scratch(self._history_path) as tmp:
                tmp.write_text(
                    "".join(
                        json.dumps(e, ensure_ascii=False) + "\n" for e in self._history
                    ),
                    encoding="utf-8",
                )
                atomic.replace(tmp, self._history_path)
        except OSError as exc:
            log.warning("window: history not rewritten: %s", exc)
        self._history_pruned_at = time.monotonic()
        self._history_sig = self._history_signature()

    def _prune_history(self) -> bool:
        cutoff = self._history_cutoff()
        before = len(self._history)
        self._history = [
            e for e in self._history if (_parse_ts(e.get("ended_at")) or 0) >= cutoff
        ]
        return len(self._history) != before

    def _record(self, entry: dict, end: str) -> None:
        """One finished grant or abandoned wait, into the 7-day log."""
        ended_at = _utcnow()
        requested = entry.get("requested_at") or entry.get("enqueued_at")
        acquired = entry.get("acquired_at")
        line = {
            "grant_id": entry.get("grant_id"),
            "cls": entry.get("cls"),
            "session": entry.get("session"),
            "instance": entry.get("instance") or "",
            "pid": entry.get("pid"),
            "label": entry.get("label") or "",
            "workers": entry.get("workers"),
            "forced": bool(entry.get("forced")),
            "requested_at": requested,
            "acquired_at": acquired,
            "ended_at": ended_at,
            "wait_seconds": _seconds_between(requested, acquired or ended_at),
            "held_seconds": _seconds_between(acquired, ended_at) if acquired else None,
            "end": end,
            "result": entry.get("result"),
        }
        self._history.append(line)
        if (
            self._prune_history()
            or time.monotonic() - self._history_pruned_at > HISTORY_PRUNE_EVERY
        ):
            self._rewrite_history()
            return
        try:
            self._history_path.parent.mkdir(parents=True, exist_ok=True)
            with self._history_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("window: history line not written: %s", exc)
        self._history_sig = self._history_signature()

    @_shared
    def report_result(self, grant_id: str, result) -> bool:
        """Attach a run's result to a live grant. False = no such holder.

        The reporter is the pytest that ran under the grant (``tests/
        conftest.py``); a nested pytest reports into its parent's grant, so
        the owner that releases it later carries the result into the log.
        """
        cleaned = clean_result(result)
        holder = next((h for h in self._holders if h["grant_id"] == grant_id), None)
        if holder is None or cleaned is None:
            return False
        holder["result"] = cleaned
        self._adapt(holder, cleaned)
        self._save()
        return True

    def _adapt(self, holder: dict, result: dict) -> None:
        """AIMD on the worker budget from one reported run (claunch-afmzw).

        Timeouts halve the budget (not below the floor); a passed run adds
        one back. A run granted before the last decrease changes nothing:
        it was measured under the load that decrease already answered, and
        letting every run of one overloaded moment halve again would take
        the budget to the floor on a single burst. Each grant adds at most
        once (a nested pytest reports into its parent's grant).
        """
        configured = self._configured_budget()
        if configured <= 0 or not self._adaptive_on():
            return
        decreased = self._adaptive.get("decreased_at")
        acquired = holder.get("acquired_ts")
        if acquired is None:
            acquired = _parse_ts(holder.get("acquired_at"))
        after_decrease = (
            decreased is None or acquired is None or acquired >= float(decreased)
        )
        current = self._budget()
        if int(result.get("timeouts") or 0) > 0:
            if not after_decrease:
                return
            lowered = max(self._adaptive_floor(), current // 2)
            if lowered >= current:
                return
            self._adaptive = {
                "budget": lowered,
                "decreased_at": time.time(),
                "reason": (
                    f"{result['timeouts']} timeout(s) in {holder.get('grant_id')} "
                    f"({holder.get('session') or 'no session'})"
                ),
            }
            log.info("window: worker budget %d -> %d after timeouts in %s",
                     current, lowered, holder.get("grant_id"))
            return
        if (
            result.get("outcome") == "passed"
            and after_decrease
            and not holder.get("credited")
            and self._adaptive.get("budget") is not None
        ):
            holder["credited"] = True
            raised = current + 1
            self._adaptive = dict(self._adaptive)
            if raised >= configured:
                self._adaptive["budget"] = None
                self._adaptive["reason"] = "recovered to the configured budget"
            else:
                self._adaptive["budget"] = raised
            log.info("window: worker budget %d -> %d after a passed run",
                     current, min(raised, configured))

    def adaptive_view(self) -> dict:
        """The AIMD state as the status answer and the CLI show it."""
        decreased = self._adaptive.get("decreased_at")
        return {
            "enabled": self._adaptive_on(),
            "budget": self._budget(),
            "configured": self._configured_budget(),
            "floor": self._adaptive_floor(),
            "lowered": self._adaptive.get("budget") is not None,
            "decreased_at": (
                datetime.fromtimestamp(float(decreased), timezone.utc).isoformat(
                    timespec="seconds"
                )
                if decreased
                else None
            ),
            "reason": self._adaptive.get("reason") or "",
        }

    @_shared
    def history(
        self,
        *,
        days: Optional[float] = None,
        cls: Optional[str] = None,
        session: Optional[str] = None,
        limit: int = 50,
    ) -> dict:
        """The kept log, newest first, with statistics over the same filter."""
        span = HISTORY_RETENTION_DAYS if not days else min(float(days), HISTORY_RETENTION_DAYS)
        cutoff = time.time() - span * 86400.0
        rows = [
            e
            for e in self._history
            if (_parse_ts(e.get("ended_at")) or 0) >= cutoff
            and (not cls or e.get("cls") == cls)
            # A session name is this instance's (claunch-afmzw).
            and (not session or self._same_session(e, session))
        ]
        return {
            "retention_days": HISTORY_RETENTION_DAYS,
            "days": span,
            "stats": self._stats(rows),
            "entries": list(reversed(rows))[: max(0, int(limit))],
            "total": len(rows),
        }

    @staticmethod
    def _stats(rows: List[dict]) -> dict:
        by_class = {}
        for cls in CLASSES:
            mine = [r for r in rows if r.get("cls") == cls]
            runs = [r for r in mine if r.get("acquired_at")]
            outcomes: Dict[str, int] = {}
            for r in runs:
                outcome = (r.get("result") or {}).get("outcome") or "unreported"
                outcomes[outcome] = outcomes.get(outcome, 0) + 1
            by_class[cls] = {
                "runs": len(runs),
                "gave_up_waiting": len(mine) - len(runs),
                "forced": sum(1 for r in runs if r.get("forced")),
                "outcomes": outcomes,
                "wait_seconds": _spread(
                    [r["wait_seconds"] for r in runs if r.get("wait_seconds") is not None]
                ),
                "held_seconds": _spread(
                    [r["held_seconds"] for r in runs if r.get("held_seconds") is not None]
                ),
            }
        sessions: Dict[str, int] = {}
        for r in rows:
            if r.get("acquired_at"):
                who = r.get("session") or f"pid {r.get('pid')}"
                sessions[who] = sessions.get(who, 0) + 1
        top = sorted(sessions.items(), key=lambda kv: (-kv[1], kv[0]))[:10]
        return {"classes": by_class, "top_sessions": [{"holder": k, "runs": v} for k, v in top]}

    # ---- reaping ------------------------------------------------------------ #

    def _session_gone(self, name: str) -> bool:
        if self._manager is None:
            return False
        try:
            session = self._manager.get(name)
        except Exception:
            return True  # the daemon never heard of it: gone
        return bool(getattr(session, "exited", False))

    def _dead(self, entry: dict, *, waiting: bool = False) -> bool:
        """A holder/waiter nobody is behind: dead session, or a sessionless
        (manual) entry whose pid is gone.

        A *bound* entry (``bound``: the requesting process holds the grant
        for its own lifetime -- a pytest run through ``test_window``) is dead
        when that pid is, whether or not its session lives on: a run killed
        by a shell ``timeout`` or a daemon restart otherwise left its grant
        held under a live session (claunch-zawnr). A grant taken with
        ``claunch window acquire`` is not bound -- that process exits at once
        and the session releases the grant later.
        """
        if entry.get("bound") and (entry.get("pid") or 0) > 0:
            if not pid_alive(int(entry["pid"])):
                return True
        if not self._mine(entry):
            # Another daemon instance's entry (claunch-afmzw): its sessions
            # are not in this manager, so their liveness is not ours to
            # judge -- that daemon's exit hook releases them. A waiter is a
            # long-poll on that daemon's process and dies with it.
            if waiting:
                owner = int(entry.get("daemon_pid") or 0)
                return owner > 0 and not pid_alive(owner)
            if entry.get("session"):
                # Its daemon judges the session; once that daemon is gone and
                # no process serves the instance any more, nobody will
                # (claunch-afmzw.3). A restarted daemon keeps its sessions
                # and judges them itself, so a live instance is left alone.
                owner = int(entry.get("daemon_pid") or 0)
                return (
                    owner > 0
                    and not pid_alive(owner)
                    and not _instance_served(entry.get("instance") or "")
                )
            pid = entry.get("pid") or 0
            return not pid_alive(pid)
        session = entry.get("session")
        if session:
            return self._session_gone(session)
        pid = entry.get("pid") or 0
        return not pid_alive(pid)

    def _reap(self) -> bool:
        """Drop dead holders and waiters. Returns True if anything changed."""
        before_h = len(self._holders)
        before_q = len(self._queue)
        dead = [h for h in self._holders if self._dead(h)]
        for holder in dead:
            self._record(holder, "reaped")
        self._holders = [h for h in self._holders if h not in dead]
        self._queue = [q for q in self._queue if not self._dead(q, waiting=True)]
        dropped = before_h - len(self._holders) + before_q - len(self._queue)
        if dropped:
            log.info("window: reaped %d dead holder/waiter entr(ies)", dropped)
        return dropped > 0

    # ---- granting ----------------------------------------------------------- #

    def _blocker(
        self,
        cls: str,
        session: Optional[str],
        ahead: List[dict],
        instance: Optional[str] = None,
    ) -> Optional[str]:
        """Why a request of this class cannot be granted now, or None.

        ``ahead`` is the part of the queue that outranks the request. The
        reason is returned to the requester, so a refused caller learns which
        rule it met instead of guessing from the holder list. ``instance`` is
        the daemon instance the requesting session runs under (None = this
        one): session names are per instance, so ``s92`` here and ``s92`` on
        another instance are two sessions.
        """
        instance = self._instance if instance is None else instance
        if cls == SWEEP:
            if self._holders:
                return "the window is held and a sweep is exclusive"
            return None
        if any(h["cls"] == SWEEP for h in self._holders):
            return "a sweep holds the window"
        if any(q["cls"] == SWEEP for q in ahead):
            return "a sweep is queued ahead (writer preference)"
        cap = self._cap(TARGETED)
        if sum(1 for h in self._holders if h["cls"] == TARGETED) >= cap:
            return f"the targeted cap ({cap}) is reached"
        per_session = int(
            self._limits().get(
                "targeted_per_session", DEFAULT_LIMITS["targeted_per_session"]
            )
        )
        if session and per_session > 0:
            mine = sum(
                1
                for h in self._holders
                if h["cls"] == TARGETED
                and h.get("session") == session
                and (h.get("instance") or "") == instance
            )
            if mine >= per_session:
                return (
                    f"session {session} already holds {mine} targeted grant(s); "
                    f"the limit is {per_session} per session"
                )
        left = self._budget_left()
        if left is not None and left < 1:
            return f"the worker budget ({self._budget()}) is spent"
        return None

    def _grantable(
        self, cls: str, session: Optional[str] = None, ahead: Optional[List[dict]] = None
    ) -> bool:
        return self._blocker(cls, session, self._queue if ahead is None else ahead) is None

    def _grant(self, entry: dict, *, forced: bool = False) -> dict:
        holder = dict(entry)
        requested = holder.pop("requested", None)
        if forced:
            # Past every limit by definition; the width still honours the
            # class ceiling and the requester's own ask.
            width = self._class_width(holder["cls"])
            holder["workers"] = min(width, int(requested)) if requested else width
            holder["forced"] = True
        else:
            holder["workers"] = self._width(holder["cls"], requested)
        holder["acquired_at"] = _utcnow()
        # Sub-second, for the AIMD's "granted before the last decrease".
        holder["acquired_ts"] = time.time()
        # Kept for the history's wait time; the queue field name goes.
        holder["requested_at"] = holder.pop("enqueued_at", None) or holder["acquired_at"]
        holder.pop("priority", None)
        self._holders.append(holder)
        event = self._granted_events.pop(entry["grant_id"], None)
        if event is not None:
            event.set()
        return holder

    @staticmethod
    def _priority(entry: dict) -> int:
        try:
            return int(entry.get("priority") or 0)
        except (TypeError, ValueError):
            return 0

    def _sort_queue(self) -> None:
        # Stable: FIFO order survives inside each priority.
        self._queue.sort(key=lambda q: -self._priority(q))

    def _ahead_of(self, priority: int) -> List[dict]:
        """The waiting entries a new request of this priority queues behind."""
        return [q for q in self._queue if self._priority(q) >= priority]

    def _process_queue(self) -> List[dict]:
        """Grant everything the queue allows, in priority-then-FIFO order.

        Writer preference falls out of the predicates: a queued sweep makes
        every targeted entry behind it ungrantable, and a sweep grants only
        into an empty window. An entry that stays blocked joins ``ahead`` for
        the entries after it.
        """
        granted = []
        ahead: List[dict] = []
        for entry in list(self._queue):
            if self._blocker(
                entry["cls"], entry.get("session"), ahead, entry.get("instance") or ""
            ) is not None:
                ahead.append(entry)
                continue
            self._queue.remove(entry)
            granted.append(self._grant(entry))
        return granted

    def advisory_n(self, extra: int = 0) -> int:
        """The width one run should take, given how many are running.

        The one number the arbiter knows that no scanning session can: how
        many runs are actually active. ``extra`` counts a grant about to be
        made, so the answer a requester gets already includes itself.
        """
        active = max(1, len(self._holders) + extra)
        return max(ADVISORY_MIN, min(ADVISORY_MAX, self._cores // active))

    # ---- the public surface ------------------------------------------------- #

    async def acquire(
        self,
        cls: str,
        *,
        session: Optional[str],
        pid: int = 0,
        label: str = "",
        wait: float = 0.0,
        workers: int = 0,
        priority: int = 0,
        force: bool = False,
        bound: bool = False,
        gone: Optional[Callable[[], bool]] = None,
    ) -> dict:
        """Grant the window, or queue for it up to ``wait`` seconds.

        ``bound`` marks a grant held for the requesting pid's lifetime
        (:meth:`_dead`). ``gone`` reports that the long-poll's client has
        disconnected; a queued request whose client is gone leaves the queue,
        and a grant made just as it left is released again, instead of being
        held for nobody (claunch-zawnr).

        A ``wait`` of 0 asks without queueing and jumps nobody: with a
        non-empty queue the answer is ``granted: False`` even into a grantable
        slot, because the slot belongs to the queue's head.

        ``workers`` is the xdist width the requester wants (0 = the class
        ceiling); the grant's ``advisory_n`` never exceeds it. ``force`` is the
        operator's immediate grant, and is refused for a session-held request.
        """
        if cls not in CLASSES:
            return {"granted": False, "error": f"unknown window class {cls!r}"}
        if isinstance(priority, bool) or not isinstance(priority, int):
            return {"granted": False, "error": "priority must be an integer"}
        if session and priority > 0:
            return {"granted": False, "error": "positive priority is an operator action"}
        if force and session:
            return {
                "granted": False,
                "error": "force is an operator action; a session-held request "
                "cannot be forced (ask the operator to run `claunch window force`)",
            }
        entry = {
            "grant_id": secrets.token_hex(6),
            "cls": cls,
            "session": session,
            "pid": pid,
            "label": label,
            "enqueued_at": _utcnow(),
            "priority": priority,
            "instance": self._instance,
            "daemon_pid": self._daemon_pid,
        }
        if bound:
            entry["bound"] = True
        if workers:
            entry["requested"] = max(1, int(workers))
        grant_id = entry["grant_id"]
        with self._locked():
            changed = self._reap()
            if changed:
                self._process_queue()
                self._save()
            if force:
                holder = self._grant(entry, forced=True)
                self._save()
                log.info("window: operator forced a %s grant (%s)", cls, grant_id)
                return {
                    "granted": True,
                    "grant_id": grant_id,
                    "advisory_n": holder["workers"],
                    "forced": True,
                }
            ahead = self._ahead_of(priority)
            blocker = self._blocker(cls, session, ahead)
            if not ahead and blocker is None:
                holder = self._grant(entry)
                self._save()
                return {
                    "granted": True,
                    "grant_id": grant_id,
                    "advisory_n": holder["workers"],
                }
            reason = blocker or f"{len(ahead)} request(s) are queued ahead"
            if wait <= 0:
                return {
                    "granted": False,
                    "position": len(ahead) + 1,
                    "reason": reason,
                    "window": self.status(),
                }
            event = asyncio.Event()
            self._granted_events[grant_id] = event
            self._queue.append(entry)
            self._sort_queue()
            # Entries ahead may be blocked by a rule that does not bind this
            # one (another session's per-session limit); the queue decides,
            # in order.
            self._process_queue()
            self._save()
        deadline = time.monotonic() + min(wait, MAX_WAIT)
        # The state is shared with every daemon instance on this machine
        # (claunch-afmzw): a grant or a release made by another process sets
        # no event here, so the wait also re-reads the state every poll.
        poll = min(GONE_POLL, SHARED_POLL) if gone else SHARED_POLL
        try:
            while not event.is_set():
                left = deadline - time.monotonic()
                if left <= 0:
                    raise asyncio.TimeoutError
                try:
                    await asyncio.wait_for(event.wait(), timeout=min(left, poll))
                except asyncio.TimeoutError:
                    pass
                if event.is_set():
                    break
                with self._locked():
                    if self._holder(grant_id) is not None:
                        break
                    if self._queued(grant_id) is None:
                        # Cancelled or reaped while waiting, here or elsewhere.
                        return {"granted": False, "cancelled": True}
                    if gone is not None and gone():
                        self._unqueue(grant_id, "disconnected")
                        self._save()
                        return {"granted": False, "disconnected": True}
                    if self._reap():
                        self._save()
                    if self._process_queue():
                        self._save()
                    if self._holder(grant_id) is not None:
                        break
                    if self._queued(grant_id) is None:
                        return {"granted": False, "cancelled": True}
        except asyncio.TimeoutError:
            with self._locked():
                if self._holder(grant_id) is None:
                    if self._unqueue(grant_id, "timeout"):
                        self._save()
                    return {
                        "granted": False,
                        "timeout": True,
                        "window": self.status(),
                    }
        except asyncio.CancelledError:
            # A disconnected long-poll must not leave an orphan that can be
            # granted later without a client to receive the grant id.
            with self._locked():
                if self._unqueue(grant_id, "disconnected"):
                    self._save()
            raise
        finally:
            self._granted_events.pop(grant_id, None)
        with self._locked():
            holder = self._holder(grant_id) or {}
            if gone is not None and gone() and holder:
                # Granted while its client was leaving: nobody will read the id.
                self._holders = [h for h in self._holders if h["grant_id"] != grant_id]
                self._record(holder, "disconnected")
                self._process_queue()
                self._save()
                return {"granted": False, "disconnected": True}
            result = {
                "granted": True,
                "grant_id": grant_id,
                "advisory_n": holder.get("workers") or self._width(cls, workers or None),
            }
            if holder.get("forced"):
                result["forced"] = True
            return result

    def _holder(self, grant_id: str) -> Optional[dict]:
        return next((h for h in self._holders if h["grant_id"] == grant_id), None)

    def _queued(self, grant_id: str) -> Optional[dict]:
        return next((q for q in self._queue if q["grant_id"] == grant_id), None)

    def _unqueue(self, grant_id: str, end: str) -> bool:
        """Take one waiter out of the queue into the log. False = not queued."""
        entry = self._queued(grant_id)
        if entry is None:
            return False
        self._queue = [q for q in self._queue if q["grant_id"] != grant_id]
        self._record(entry, end)
        return True

    @_shared
    def release(self, grant_id: str) -> bool:
        """Hand the window back. False = no such grant (already reaped?)."""
        holder = next((h for h in self._holders if h["grant_id"] == grant_id), None)
        if holder is None:
            return False
        self._holders.remove(holder)
        self._record(holder, "released")
        self._process_queue()
        self._save()
        return True

    @_shared
    def release_session(self, session: str) -> int:
        """Everything this session holds, back. Returns how many."""
        ids = [h["grant_id"] for h in self._holders if self._same_session(h, session)]
        return sum(1 for gid in ids if self.release(gid))

    @_shared
    def cancel(self, grant_id: str) -> bool:
        """Remove one waiting request.  A holder cannot be cancelled here."""
        entry = next((q for q in self._queue if q["grant_id"] == grant_id), None)
        if entry is None:
            return False
        self._queue.remove(entry)
        self._granted_events.pop(grant_id, None)
        self._record(entry, "cancelled")
        self._save()
        return True

    @_shared
    def cancel_session(self, session: str) -> int:
        """Drop every waiting request from this session (a waiter giving up)."""
        entries = [q for q in self._queue if self._same_session(q, session)]
        for entry in entries:
            self._queue.remove(entry)
            self._granted_events.pop(entry["grant_id"], None)
            self._record(entry, "cancelled")
        if entries:
            self._save()
        return len(entries)

    # ---- operator overrides (claunch-8kald) ------------------------------------ #

    @_shared
    def prioritize(self, grant_id: str, priority: Optional[int] = None) -> Optional[dict]:
        """Give one waiting request a priority; None = move it to the top.

        Higher priorities are granted first and FIFO holds inside a priority.
        The queue is processed right away, so a request moved above a queued
        sweep may be granted on the spot. Returns None for no such request.
        """
        entry = next((q for q in self._queue if q["grant_id"] == grant_id), None)
        if entry is None:
            return None
        if priority is None:
            others = [self._priority(q) for q in self._queue if q is not entry]
            priority = max(others + [self._priority(entry) - 1]) + 1
        entry["priority"] = int(priority)
        self._sort_queue()
        self._process_queue()
        self._save()
        granted = any(h["grant_id"] == grant_id for h in self._holders)
        position = (
            None
            if granted
            else next(i for i, q in enumerate(self._queue, 1) if q["grant_id"] == grant_id)
        )
        log.info(
            "window: operator set priority %d on %s (%s)",
            entry["priority"], grant_id, "granted" if granted else f"position {position}",
        )
        return {"priority": entry["priority"], "granted": granted, "position": position}

    @_shared
    def force(self, grant_id: str) -> Optional[dict]:
        """Grant one waiting request now, past every limit. None = no such request.

        The running holders keep running; the forced holder is added next to
        them and counts against the caps and the budget for everyone after.
        """
        entry = next((q for q in self._queue if q["grant_id"] == grant_id), None)
        if entry is None:
            return None
        self._queue.remove(entry)
        holder = self._grant(entry, forced=True)
        self._save()
        log.info("window: operator forced %s (%s)", grant_id, holder["cls"])
        return holder

    @_shared
    def limits_view(self) -> dict:
        """Who sets the limits and what is in force (claunch-k7x63)."""
        return {
            "values": self._all_limits(),
            "overrides": dict(self._overrides),
            "editable": self._owns_limits,
            # "published": a sub daemon enforcing the main daemon's limits;
            # "own": the owner, or a sub daemon nobody has published to yet.
            "source": (
                "own" if self._owns_limits or not self._published else "published"
            ),
            "by": self._limits_meta.get("by")
            or (self._instance or "default" if self._owns_limits else None),
            "set_by": self._limits_meta.get("set_by"),
            "set_at": self._limits_meta.get("set_at"),
            "bounds": {k: list(v) for k, v in LIMIT_BOUNDS.items()},
        }

    @_shared
    def set_limits(self, changes: dict, *, by: str = "") -> dict:
        """Override limits on the daemon that owns them (the main one).

        A value replaces the config key it overrides; ``None`` drops the
        override so the config default applies again. Raises
        :class:`LimitsNotEditable` on a daemon that enforces another
        daemon's limits, and ValueError on a bad request (nothing applied).
        """
        if not self._owns_limits:
            raise LimitsNotEditable(
                "the test window's limits are set on the main daemon "
                f"({self._limits_meta.get('by') or 'default'}); this daemon "
                f"({self._instance or 'default'}) shares its window and queue"
            )
        cleaned = validate_limits(changes)
        before = self._all_limits()
        for key, value in cleaned.items():
            if value is None:
                self._overrides.pop(key, None)
            else:
                self._overrides[key] = value
        self._limits_meta["set_by"] = by or None
        self._limits_meta["set_at"] = _utcnow()
        after = self._all_limits()
        # A lowered budget no longer above the new configured one is moot.
        current = self._adaptive.get("budget")
        if current is not None and current >= self._configured_budget():
            self._adaptive["budget"] = None
        # A raised cap or budget may admit a waiter now.
        self._process_queue()
        self._save()
        changed = {
            k: [before.get(k), after.get(k)]
            for k in after
            if before.get(k) != after.get(k)
        }
        log.info("window: limits changed by %s: %s", by or "?", changed)
        return self.limits_view()

    @_shared
    def status(self) -> dict:
        """The window as state anyone can read -- the whole point.

        Deliberately does not reap: status is a read, and a read with side
        effects is how a quiet ``status`` call ends up releasing somebody's
        grant. Reaping happens where grants are decided (``acquire``).
        """
        sweep_cap, targeted_cap = self._caps()
        return {
            "holders": list(self._holders),
            "queue": list(self._queue),
            "caps": {"sweep": sweep_cap, "targeted": targeted_cap},
            "max_wait": MAX_WAIT,
            "reminder_interval": REMINDER_INTERVAL,
            "cores": self._cores,
            # The width a targeted request made now would be granted.
            "advisory_n_now": self._width(TARGETED, None),
            "limits": dict(self._limits()),
            "workers_in_use": self.workers_in_use(),
            # The budget in force (claunch-afmzw): below limits.worker_budget
            # while timeouts have lowered it.
            "worker_budget": self._budget(),
            "adaptive": self.adaptive_view(),
            "limits_control": self.limits_view(),
            "instance": self._instance,
            "state_path": str(self._state_path),
            "history_retention_days": HISTORY_RETENTION_DAYS,
        }

    # ---- one session's view (the terminal header's strip) ------------------ #

    @_shared
    def estimate(self, cls: str, label: str = "") -> Optional[dict]:
        """How long a grant like this one usually holds, from the kept log.

        Same class and label first -- a label names the command, and one
        module's run says nothing about the full suite's -- then the class.
        ``ESTIMATE_MIN_SAMPLES`` runs are needed before a median is offered:
        one past run is an anecdote, and a countdown built on it is a guess
        dressed as a measurement. Runs that ended with their client gone are
        left out; their hold is not the run's length.
        """
        runs = [
            r
            for r in self._history
            if r.get("cls") == cls
            and r.get("held_seconds") is not None
            and r.get("end") != "disconnected"
        ]
        candidates = []
        if label:
            candidates.append(("label", [r for r in runs if (r.get("label") or "") == label]))
        candidates.append(("class", runs))
        for basis, rows in candidates:
            if len(rows) < ESTIMATE_MIN_SAMPLES:
                continue
            held = [float(r["held_seconds"]) for r in rows]
            return {
                "basis": basis,
                "samples": len(held),
                "p50": _quantile(held, 0.5),
                "p90": _quantile(held, 0.9),
            }
        return None

    def _brief_holder(self, holder: dict, now: float) -> dict:
        acquired = _parse_ts(holder.get("acquired_at"))
        elapsed = round(max(0.0, now - acquired), 1) if acquired is not None else None
        estimate = self.estimate(holder.get("cls", TARGETED), holder.get("label") or "")
        remaining = None
        if estimate and estimate.get("p50") is not None and elapsed is not None:
            remaining = round(estimate["p50"] - elapsed, 1)
        return {
            "grant_id": holder.get("grant_id"),
            "cls": holder.get("cls"),
            "session": holder.get("session"),
            "instance": holder.get("instance") or "",
            "label": holder.get("label") or "",
            "workers": holder.get("workers"),
            "forced": bool(holder.get("forced")),
            "acquired_at": holder.get("acquired_at"),
            "elapsed_seconds": elapsed,
            "estimate": estimate,
            # Negative once the run has outlived its median: the strip says
            # "over", it does not count below zero.
            "remaining_seconds": remaining,
            "result": holder.get("result"),
        }

    @_shared
    def session_view(self, session: str, now: Optional[float] = None) -> dict:
        """The window as it concerns ONE session: what it holds, where it waits.

        A read, like :meth:`status` -- it reaps nothing. ``holding`` carries
        the elapsed time and the history's median for a grant like it;
        ``waiting`` carries the queue position, how many are ahead, how long
        it has waited and the rule that keeps it out (the same words
        :meth:`acquire` gives the requester). ``window`` is the whole
        machine in a few numbers, with every holder's elapsed/remaining, so
        a waiter can see whom it waits on. ``last`` is the session's most
        recent finished run in the kept log.
        """
        now = time.time() if now is None else now
        holders = [self._brief_holder(h, now) for h in self._holders]
        holding = [h for h in holders if self._same_session(h, session)]
        waiting = []
        for index, entry in enumerate(self._queue):
            if not self._same_session(entry, session):
                continue
            ahead = self._queue[:index]
            enqueued = _parse_ts(entry.get("enqueued_at"))
            reason = self._blocker(entry["cls"], session, ahead)
            if reason is None:
                reason = (
                    f"{len(ahead)} request(s) are queued ahead"
                    if ahead
                    else "about to be granted"
                )
            waiting.append(
                {
                    "grant_id": entry.get("grant_id"),
                    "cls": entry.get("cls"),
                    "label": entry.get("label") or "",
                    "priority": self._priority(entry),
                    "position": index + 1,
                    "ahead": len(ahead),
                    "enqueued_at": entry.get("enqueued_at"),
                    "waited_seconds": (
                        round(max(0.0, now - enqueued), 1) if enqueued is not None else None
                    ),
                    "reason": reason,
                    "estimate": self.estimate(entry["cls"], entry.get("label") or ""),
                }
            )
        last = next(
            (r for r in reversed(self._history) if self._same_session(r, session)),
            None,
        )
        sweep_cap, targeted_cap = self._caps()
        budget = self._budget()
        return {
            "session": session,
            "holding": holding,
            "waiting": waiting,
            "last": last,
            "window": {
                "holders": holders,
                "queued": len(self._queue),
                "sweep_queued": any(q["cls"] == SWEEP for q in self._queue),
                "caps": {"sweep": sweep_cap, "targeted": targeted_cap},
                "workers_in_use": self.workers_in_use(),
                "worker_budget": budget if budget > 0 else None,
            },
        }

    # ---- the exit hook -------------------------------------------------------- #

    @_shared
    def session_exited(self, session) -> None:
        """The manager's exit funnel: a session that ends gives the window back.

        Same funnel, same contract as ``daemon/beads.py``'s hook: called
        synchronously in the loop, never awaited. A session whose pytest is
        mid-run when its terminal dies is exactly the stale-holder case this
        hook exists for -- the child pytest dying with its session is what
        makes releasing right, and if it somehow outlives the session the pid
        reaper at the next acquire finishes the job.
        """
        name = session.sdef.name
        held = [h for h in self._holders if self._same_session(h, name)]
        queued = [q for q in self._queue if self._same_session(q, name)]
        if not held and not queued:
            return
        for entry in held + queued:
            self._record(entry, "session_exited")
        self._holders = [h for h in self._holders if not self._same_session(h, name)]
        self._queue = [q for q in self._queue if not self._same_session(q, name)]
        self._process_queue()
        self._save()


def reminder_block(holder: dict) -> str:
    """The independent reminder sent while a session keeps a test grant."""
    grant_id = holder.get("grant_id") or "?"
    cls = holder.get("cls") or "unknown"
    label = holder.get("label") or "no label"
    acquired = holder.get("acquired_at") or "unknown time"
    return "\n".join(
        [
            "---",
            "# claunch window: release reminder -- machine-generated, not typed by "
            "the user; repeats every 3 minutes while this grant is held",
            f"grant: {grant_id} ({cls})",
            f"held since: {acquired}",
            f"label: {label}",
            "protocol: this test window is still held. If the test completed or "
            "failed, release it now with `claunch window release --grant-id "
            f"{grant_id}`. If it is still running, keep the grant and continue "
            "with its result.",
            "---",
        ]
    )


class WindowReminderClock:
    """Remind live session holders to release the measurement window.

    The clock has no authority to release a live holder: a process can still
    be running after its terminal stops producing output, and releasing such
    a grant would permit an overlapping measurement.  Session exit and PID
    reaping remain the release mechanisms.  This clock supplies the missing
    prompt after failed tests leave a live session holding a grant.
    """

    def __init__(
        self,
        manager,
        window: WindowManager,
        *,
        interval: float = REMINDER_INTERVAL,
        poll: float = REMINDER_POLL,
    ) -> None:
        self.manager = manager
        self.window = window
        self.interval = interval
        self.poll = poll
        self._seen: Dict[str, float] = {}
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.poll)
                for holder in self.scan(time.monotonic()):
                    await self._deliver(holder)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("window reminder clock tick failed")

    def scan(self, now: Optional[float] = None) -> List[dict]:
        """Return session-held grants whose next 3-minute reminder is due."""
        stamp = time.monotonic() if now is None else now
        status = self.window.status()
        holders = status.get("holders") or []
        here = status.get("instance") or ""
        live = set()
        due: List[dict] = []
        for holder in holders:
            grant_id = str(holder.get("grant_id") or "")
            session = holder.get("session")
            if not grant_id or not session:
                continue
            if (holder.get("instance") or "") != here:
                # Another daemon instance's session: its name means nothing
                # in this manager (claunch-afmzw).
                continue
            live.add(grant_id)
            armed_at = self._seen.setdefault(grant_id, stamp)
            if stamp - armed_at >= self.interval:
                due.append(holder)
        for grant_id in list(self._seen):
            if grant_id not in live:
                del self._seen[grant_id]
        return due

    async def _deliver(self, holder: dict) -> None:
        """Deliver one due reminder; only a successful delivery re-arms it."""
        session_name = str(holder.get("session") or "")
        grant_id = str(holder.get("grant_id") or "")
        if not session_name or not grant_id:
            return
        try:
            session = self.manager.get(session_name)
        except Exception:
            return
        if getattr(session, "exited", False):
            return
        try:
            delivered = await session.deliver(reminder_block(holder))
        except Exception:
            log.exception("window reminder delivery to %r failed", session_name)
            return
        if delivered:
            self._seen[grant_id] = time.monotonic()
            log.info("window release reminder delivered to %r (%s)", session_name, grant_id)
