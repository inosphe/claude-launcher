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

import contextlib
import os
import re
import subprocess
import sys
import time
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Iterator, Mapping, Optional, Sequence

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
#: Beside ``WINDOW_REPORT_ENV``: a file the reporting pytest writes its
#: :func:`pytest_result` to instead of posting it, for an owner that runs the
#: suite where the daemon is out of reach (tools/sweep.py hands the suite a
#: throwaway launcher home, claunch-gziru) and posts the result itself.
WINDOW_RESULT_FILE_ENV = "CLAUNCH_WINDOW_RESULT_FILE"
#: The session a sessionless run works for (a delayed sweep runs without
#: ``CLAUNCH_SESSION`` so it outlives its leader). Shown on the Window page and
#: in the run history; the daemon never treats it as the grant's owner.
WINDOW_REQUESTED_BY_ENV = "CLAUNCH_WINDOW_REQUESTED_BY"
#: ``0`` keeps a run from being offered to other machines (claunch-ohsyx).
WINDOW_BORROW_ENV = "CLAUNCH_WINDOW_BORROW"

DEFAULT_WAIT = 1800.0
DEFAULT_SWEEP_WORKERS = 8
DEFAULT_TARGETED_WORKERS = 4

#: The fallback's targeted capacity: the daemon's default targeted cap.
FALLBACK_TARGETED_SLOTS = 3


class WindowUnavailable(RuntimeError):
    """The arbiter answered but did not grant the requested window."""


class RemoteResult(Exception):
    """Another machine ran this request and sent its result (claunch-ohsyx).

    Raised by :func:`acquire` instead of returning a grant: there is nothing
    left to run here. ``remote`` carries ``exit_code``, the output's tail,
    the junit report and the window result; :func:`replay_remote` turns it
    into this pytest's own ending.
    """

    def __init__(self, remote: dict):
        self.remote = remote
        try:
            self.exit_code = int(remote.get("exit_code"))
        except (TypeError, ValueError):
            self.exit_code = 3
        super().__init__(
            f"this run was borrowed by {remote.get('machine') or 'another machine'} "
            f"and finished there with exit {self.exit_code}"
        )


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
    priority: int = 0,
    borrow_args: Optional[Sequence[str]] = None,
    borrow_cwd: Optional[str] = None,
) -> WindowGrant:
    """Acquire a daemon grant, or the documented deployment fallback.

    ``workers`` is the xdist width the run wants (0 = the class ceiling); a
    run that will not use xdist should say 1, so the machine's worker budget
    is charged what the run actually spends.

    ``borrow_args`` are the run's pytest arguments. With them a queued
    request may be borrowed by another machine (claunch-ohsyx): the answer
    is then that machine's result, raised as :class:`RemoteResult`.
    ``borrow_cwd`` is where the run runs (default: this process's cwd).
    """
    if cls not in CLASSES:
        raise ValueError(f"unknown test window class {cls!r}")
    if isinstance(priority, bool) or not isinstance(priority, int):
        raise ValueError("priority must be an integer")
    inherited = inherited_grant()
    if inherited is not None:
        if priority < 0:
            raise WindowUnavailable("background tests must request their own priority grant")
        if cls == SWEEP and inherited.cls != SWEEP:
            raise WindowUnavailable(
                "a targeted parent grant cannot cover an exclusive sweep"
            )
        return inherited

    owner = session or os.environ.get("CLAUNCH_SESSION")
    if os.environ.get(WINDOW_MODE_ENV, "").strip().lower() == "off":
        if priority < 0:
            raise WindowUnavailable("background tests require an enabled daemon priority queue")
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
            # This process holds the grant for its lifetime and releases it on
            # exit; a grant left by a run that was killed is reaped by its pid
            # (claunch-zawnr).
            "bound": True,
        }
        if workers:
            body["workers"] = max(1, int(workers))
        if priority:
            body["priority"] = priority
        requested_by = os.environ.get(WINDOW_REQUESTED_BY_ENV, "").strip()
        if requested_by and not owner:
            body["requested_by"] = requested_by
        if borrow_args is not None and wait > 0 and _borrow_on():
            info = borrow_info(borrow_args, cwd=borrow_cwd)
            if info is not None:
                body["borrow"] = info
        # Said before the POST, which blocks until the grant or the budget runs
        # out. Without it a run that queues on the machine's window and is then
        # killed by its own shell `timeout` leaves no output at all, and
        # "waiting for the window" is indistinguishable from "hung" — measured
        # 2026-10-06: a six-module run waited 1187s for its grant under an 800s
        # timeout, and the session that ran it diagnosed a stalled checkout and
        # wrote that into a round report. Flushed, so the line survives the
        # kill. The wait itself is untouched here: the caps, the queue order
        # and the budget are the daemon's.
        if wait > 0:
            print(
                f"waiting for the {cls} test window (up to {wait:.0f}s)…",
                file=sys.stderr,
                flush=True,
            )
        try:
            result = client.post(
                "/api/window/acquire",
                body,
                timeout=max(5.0, wait + 5.0),
            )
        except daemon_client.DaemonClientError as exc:
            if priority < 0:
                raise WindowUnavailable("background tests require the daemon priority queue") from exc
            return _fallback(cls, owner, started, wait, reason=str(exc))
        if result.get("borrowed"):
            result = _await_borrowed(client, result, cls, owner, started, wait, priority)
            if isinstance(result, WindowGrant):
                return result
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
        # The elapsed half is what turns "not granted" into a measurement: a
        # refusal that came back at once and one that spent the whole budget
        # queued read the same without it.
        raise WindowUnavailable(
            f"test window {cls} {detail} after {time.monotonic() - started:.0f}s"
        )

    if priority < 0:
        raise WindowUnavailable("background tests require the daemon priority queue")
    return _fallback(cls, owner, started, wait, reason="daemon unavailable")


# ---- remote borrowing (claunch-ohsyx) --------------------------------------- #

#: How long one await step may keep the connection (daemon/window.py
#: ``AWAIT_STEP``), and how many failed steps in a row end the wait.
AWAIT_STEP = 60.0
AWAIT_RETRIES = 3

#: pytest options whose value is a path of this machine's run, not of the
#: test selection; another machine sets its own.
_MACHINE_OPTIONS = ("--basetemp", "--junit-xml", "--junitxml", "--result-log")


def _borrow_on() -> bool:
    return os.environ.get(WINDOW_BORROW_ENV, "").strip().lower() not in ("0", "off", "no", "false")


def _git_out(cwd: Path, *args: str) -> Optional[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(cwd), *args], capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=30,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    return proc.stdout.strip() if proc.returncode == 0 else None


def portable_args(args: Sequence[str], cwd: Path, top: Path) -> Optional[list]:
    """``args`` as another checkout of the repository can run them, or None.

    The machine's own outputs (basetemp, a junit path) are dropped; an
    absolute path inside the repository becomes relative to ``cwd``; one
    outside it cannot be reproduced elsewhere, so the run is not portable.
    """
    out: list = []
    skip = False
    for raw in [str(a) for a in args]:
        if skip:
            skip = False
            continue
        name, eq, value = raw.partition("=")
        if name in _MACHINE_OPTIONS:
            skip = not eq
            continue
        target = value if (eq and raw.startswith("-")) else raw
        path_part = target.split("::", 1)[0]
        if path_part and os.path.isabs(path_part):
            try:
                rel = Path(path_part).resolve().relative_to(top)
            except ValueError:
                return None
            fixed = os.path.relpath(top / rel, cwd).replace(os.sep, "/")
            target = fixed + target[len(path_part):]
            raw = f"{name}={target}" if (eq and raw.startswith("-")) else target
        out.append(raw)
    return out


def borrow_info(args: Sequence[str], cwd: Optional[str] = None) -> Optional[dict]:
    """What another machine needs to run this pytest in its own checkout.

    None outside a git repository. A dirty tree is still described, with
    ``dirty``: the lender's page then says why it cannot be borrowed.
    """
    here = Path(cwd or os.getcwd()).resolve()
    top_s = _git_out(here, "rev-parse", "--show-toplevel")
    if not top_s:
        return None
    top = Path(top_s).resolve()
    head = _git_out(top, "rev-parse", "HEAD")
    tree = _git_out(top, "rev-parse", "HEAD^{tree}")
    status = _git_out(top, "status", "--porcelain")
    if not head or not tree or status is None:
        return None
    try:
        rel = here.relative_to(top).as_posix()
    except ValueError:
        return None
    return {
        "v": 1,
        "toplevel": str(top),
        "rel_cwd": "" if rel == "." else rel,
        "head": head,
        "tree": tree,
        "dirty": bool(status),
        "pytest_args": portable_args(args, here, top),
    }


def pytest_args_of(command: str) -> Optional[list]:
    """The pytest arguments of a shell command line that runs pytest
    (``uv run --no-sync pytest ...``, ``<python> -m pytest ...``); None for
    any other command."""
    import shlex

    try:
        words = shlex.split(command, posix=os.name != "nt")
    except ValueError:
        return None
    for index, word in enumerate(words):
        stem = Path(word.strip('"')).name.lower()
        if stem in ("pytest", "pytest.exe", "py.test"):
            return words[index + 1:]
        if word == "-m" and index + 1 < len(words) and words[index + 1] == "pytest":
            return words[index + 2:]
    return None


def requested_width_of(args: Sequence[str]) -> int:
    """The xdist width ``args`` ask for: 1 without ``-n``, 0 for ``auto``."""
    args = [str(a) for a in args]
    for index, arg in enumerate(args):
        value = None
        if arg in ("-n", "--numprocesses") and index + 1 < len(args):
            value = args[index + 1]
        elif arg.startswith("--numprocesses="):
            value = arg.split("=", 1)[1]
        elif arg.startswith("-n") and len(arg) > 2:
            value = arg[2:]
        if value is not None:
            return int(value) if value.isdigit() else 0
    return 1


def _await_borrowed(client, answer: dict, cls: str, owner: Optional[str],
                    started: float, wait: float, priority: int):
    """Wait for a borrowed request: its remote result, or a grant here once
    the borrow ends. Returns a grant or the final answer dict; raises
    :class:`RemoteResult` for a result."""
    grant_id = str(answer.get("grant_id") or "")
    seen = None
    failures = 0
    while True:
        borrowed = answer.get("borrowed")
        if borrowed:
            line = (borrowed.get("machine"), borrowed.get("phase"))
            if line != seen:
                seen = line
                print(f"test window: this run is borrowed by {line[0]} ({line[1]}); "
                      "waiting for its result…", file=sys.stderr, flush=True)
        elif seen is not None:
            seen = None
            print("test window: the borrow ended; waiting here again…",
                  file=sys.stderr, flush=True)
        try:
            answer = client.post("/api/window/await",
                                 {"grant_id": grant_id, "wait": AWAIT_STEP},
                                 timeout=AWAIT_STEP + 15.0)
            failures = 0
        except daemon_client.DaemonClientError as exc:
            failures += 1
            if failures < AWAIT_RETRIES:
                time.sleep(5.0)
                continue
            if priority < 0:
                raise WindowUnavailable("background tests require the daemon priority queue") from exc
            left = max(0.0, wait - (time.monotonic() - started))
            return _fallback(cls, owner, started, left, reason=str(exc))
        if answer.get("remote"):
            raise RemoteResult(answer["remote"])
        if answer.get("pending"):
            continue
        return answer


def replay_remote(config, remote: dict, *, result_file: Optional[str] = None) -> None:
    """End this pytest as the borrowed run ended: its output, its junit
    report at this run's ``--junit-xml``, its window result in the result
    file, and its exit code. Does not return (``pytest.exit``)."""
    import json

    import pytest

    machine = remote.get("machine") or "another machine"
    header = (f"claunch window: this run was borrowed and ran on {machine} "
              f"({remote.get('platform') or '?'}, Python {remote.get('python') or '?'}, "
              f"{remote.get('recipe') or '?'} recipe, tree {str(remote.get('tree') or '')[:12]})")
    text = remote.get("output") or ""
    if remote.get("output_truncated"):
        text = "[… earlier output cut; the tail follows]\n" + text
    capman = config.pluginmanager.get_plugin("capturemanager")
    ctx = capman.global_and_fixture_disabled() if capman is not None else contextlib.nullcontext()
    with ctx:
        sys.stdout.write(header + "\n" + text + ("" if text.endswith("\n") else "\n"))
        sys.stdout.flush()
    xmlpath = getattr(config.option, "xmlpath", None)
    if xmlpath:
        try:
            from _pytest.junitxml import xml_key

            xml = config.stash.get(xml_key, None)
            if xml is not None:
                # Its sessionfinish would write an empty report over this one.
                config.pluginmanager.unregister(xml)
        except Exception:  # noqa: BLE001 — a pytest without that internal
            pass
        if remote.get("junit"):
            Path(xmlpath).parent.mkdir(parents=True, exist_ok=True)
            Path(xmlpath).write_text(remote["junit"], encoding="utf-8")
    if result_file and remote.get("window_result"):
        Path(result_file).write_text(json.dumps(remote["window_result"]), encoding="utf-8")
    code = RemoteResult(remote).exit_code
    pytest.exit(f"borrowed run on {machine} finished with exit {code}", returncode=code)


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
    # The base home's daemon directory, not this instance's: the fallback
    # guards the machine, like the daemon's window it stands in for
    # (claunch-afmzw).
    path = paths.base_home() / "daemon" / name
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


#: What a failed test's report says when it failed by running out of time:
#: an exception type (``TimeoutError``, ``asyncio.TimeoutError``,
#: ``subprocess.TimeoutExpired``, ``concurrent.futures.TimeoutError``) or a
#: helper's wait-loop message ("timed out waiting for ..."). The window lowers
#: its worker budget on these (claunch-afmzw), so a false positive costs
#: parallelism, not a verdict.
TIMEOUT_PATTERN = re.compile(
    r"\b(?:\w+\.)*Timeout(?:Error|Expired)\b|\btimed[ -]out\b|\btimeout (?:after|waiting|expired)\b",
    re.IGNORECASE,
)


def count_timeouts(reports) -> int:
    """How many failed/errored test reports read as timeouts."""
    count = 0
    for report in reports or ():
        text = getattr(report, "longreprtext", None)
        if text is None:
            text = str(getattr(report, "longrepr", "") or "")
        # Only pytest's "E" lines (the exception and its message): the rest
        # of a traceback quotes source, and source that merely handles a
        # TimeoutError is not a timeout.
        errors = [ln for ln in (text or "").splitlines() if ln.startswith("E ")]
        if TIMEOUT_PATTERN.search("\n".join(errors) if errors else (text or "")):
            count += 1
    return count


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
        "timeouts": count_timeouts(
            list(stats.get("failed", ())) + list(stats.get("error", ()))
        ),
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
