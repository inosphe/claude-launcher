"""CLI-side client for the session daemon (stdlib only — fast to import).

Discovery: the daemon writes ``daemon.json`` (host/port/pid) and a ``token``
file under ``<launcher home>/daemon/``; the client reads both, so auth is
automatic for the local user. When the daemon is not running, session commands
auto-start it (tmux-style): spawn ``python -m claude_launcher.daemon`` fully
detached, then poll ``/api/health`` until it answers. Two racing CLIs may both
spawn — the daemon's singleton lock picks one winner and the loser exits, so
both clients converge.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from typing import Optional

from .daemon import paths, runtime_state

#: How long auto-start waits for the daemon to come up.
START_TIMEOUT = 15.0

#: One health probe's timeout. Every other diagnosis duration derives from
#: this (the tests' too), so "how slow is too slow" lives in exactly one place.
HEALTH_TIMEOUT = 1.0

#: How long :func:`diagnose` must watch before it may say WEDGED. Tied to
#: START_TIMEOUT by internal consistency rather than by measurement:
#: ``ensure_running`` grants a starting daemon this long to answer its first
#: health check, so a diagnosis that judged sooner would be calling daemons
#: wedged in situations claunch's own start path waits out as normal.
VERDICT_BUDGET = START_TIMEOUT

#: Default gap between two probes of one diagnosis.
PROBE_GAP = 0.5

#: Budget for callers that only want an observation ("did it answer just
#: now?"): two probes' worth. More time would buy nothing an observation is
#: allowed to claim -- anything longer is the verdict path's job.
OBSERVATION_BUDGET = 2 * (HEALTH_TIMEOUT + PROBE_GAP)


class DaemonClientError(Exception):
    """Raised for daemon-unreachable and API-error conditions."""


class DaemonClient:
    def __init__(self, base_url: str, token: str) -> None:
        self.base_url = base_url.rstrip("/")
        self.token = token

    # ------------------------------------------------------------------ #
    # transport
    # ------------------------------------------------------------------ #
    def request(
        self,
        method: str,
        path: str,
        body: Optional[dict] = None,
        *,
        timeout: float = 30.0,
        raw: bool = False,
    ):
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(url, data=data, method=method)
        req.add_header("Authorization", f"Bearer {self.token}")
        if data is not None:
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                payload = resp.read()
        except urllib.error.HTTPError as exc:
            detail = _error_detail(exc)
            raise DaemonClientError(f"{method} {path}: {detail}") from exc
        except (urllib.error.URLError, OSError) as exc:
            raise DaemonClientError(f"cannot reach daemon at {self.base_url}: {exc}") from exc
        if raw:
            return payload
        try:
            return json.loads(payload.decode("utf-8"))
        except ValueError:
            return {}

    def get(self, path: str, **kw):
        return self.request("GET", path, **kw)

    def post(self, path: str, body: Optional[dict] = None, **kw):
        return self.request("POST", path, body if body is not None else {}, **kw)

    def put(self, path: str, body: Optional[dict] = None, **kw):
        return self.request("PUT", path, body if body is not None else {}, **kw)

    def patch(self, path: str, body: Optional[dict] = None, **kw):
        return self.request("PATCH", path, body if body is not None else {}, **kw)

    def delete(self, path: str, **kw):
        return self.request("DELETE", path, **kw)


def _error_detail(exc: urllib.error.HTTPError) -> str:
    try:
        doc = json.loads(exc.read().decode("utf-8"))
        if isinstance(doc, dict) and doc.get("error"):
            return str(doc["error"])
        if isinstance(doc, dict) and doc.get("timeout"):
            return "timed out"
    except Exception:
        pass
    return f"HTTP {exc.code}"


# --------------------------------------------------------------------------- #
# discovery / auto-start
# --------------------------------------------------------------------------- #
def _base_url(doc: dict) -> str:
    host = str(doc.get("host") or "127.0.0.1")
    # A wildcard bind is reachable locally via loopback.
    connect_host = "127.0.0.1" if host in ("0.0.0.0", "::") else host
    return f"http://{connect_host}:{doc.get('port')}"


def _health_ok(base_url: str) -> bool:
    try:
        req = urllib.request.Request(base_url + "/api/health")
        with urllib.request.urlopen(req, timeout=HEALTH_TIMEOUT) as resp:
            return resp.status == 200
    except Exception:
        return False


def is_serving() -> bool:
    """True when an announced daemon actually answers health checks."""
    doc = runtime_state.read_daemon_json()
    return bool(doc) and _health_ok(_base_url(doc))


def connect_with_diagnosis(
    *, budget: Optional[float] = None, gap: float = PROBE_GAP
) -> tuple[Optional[DaemonClient], dict]:
    """A client for the running daemon, plus the evidence behind the answer.

    Exists because a single failed probe is not an absence, and this function's
    one-line predecessor said it was. It sent exactly one health check with a
    one-second timeout; ``/api/health`` has a 13ms median but the daemon runs
    one event loop, so any turn of it that blocks takes the whole HTTP surface
    with it for as long as it lasts. Measured against a live daemon: 6 of 100
    connects came back empty while the other 94 in the same loop proved it was
    up, and one probe was seen taking 2.667s. Every claunch surface read those
    six as "daemon is not running" -- a sentence whose natural next move is to
    start a replacement, aimed at a daemon that was fine.

    So the answer is not a bool any more. The report distinguishes what the
    caller has to act on differently:

    - :data:`SERVING` -- a client, and nothing to report;
    - :data:`NOT_RUNNING` / :data:`STALE_RECORD` -- genuinely absent, decided
      from the record rather than from silence (nothing announced, or an
      announcement whose pid is gone). No patience is spent on either: they
      are not ambiguous;
    - :data:`UNRESPONSIVE` -- announced, alive, and did not answer inside the
      budget. An observation. It may be busy, and from out here busy and stuck
      look identical, so this must never be phrased as an absence.

    The budget defaults to :data:`OBSERVATION_BUDGET`, which is what that
    constant is for: two probes' worth is enough to outlast the stall that
    caused the wrong answers, and no short look is allowed to claim more.
    Only the failing path pays it -- an answering daemon returns on probe one.
    """
    report = diagnose(
        budget=OBSERVATION_BUDGET if budget is None else budget, gap=gap
    )
    if report["state"] != SERVING:
        return None, report
    token = runtime_state.load_or_create_token()
    return DaemonClient(report["base_url"], token), report


def connect() -> Optional[DaemonClient]:
    """A client for the running daemon, or None if it isn't up.

    The bool-shaped view of :func:`connect_with_diagnosis`, for callers whose
    next move is the same either way. A caller that *reports* the failure to a
    person wants the other one: only the report tells "not running" apart from
    "did not answer", and only the first of those should send anyone to start
    a daemon.
    """
    return connect_with_diagnosis()[0]


def is_absent(report: dict) -> bool:
    """Whether ``report`` establishes that no daemon is running.

    True only for the two states decided from the record itself. Silence is
    never enough: a live pid that answered nothing is unconfirmed, not absent.
    """
    return report.get("state") in (NOT_RUNNING, STALE_RECORD)


def unreachable_reason(report: dict) -> str:
    """How to tell a person why there is no client, without overclaiming.

    The whole mitigation lives in this sentence. "daemon is not running" is
    an instruction as much as a description -- its reader starts a daemon --
    so it is reserved for the states that actually establish absence. Silence
    from a live process gets the facts instead: how hard we looked, and which
    pid is sitting there so the reader can check for themselves.
    """
    state = report.get("state")
    if state == NOT_RUNNING:
        return "daemon is not running"
    pid = report.get("pid")
    if state == STALE_RECORD:
        return f"daemon is not running (daemon.json names pid {pid}, which is gone)"
    since = report.get("started_at")
    up = f"pid {pid} up since {since}" if since else f"pid {pid}"
    return (
        f"daemon did not answer ({report.get('probes')} probe(s) over "
        f"{float(report.get('budget') or 0.0):.1f}s) -- it may be busy; "
        f"daemon.json says {up}"
    )


def spawn_daemon(env: Optional[dict] = None) -> None:
    """Start the daemon as a fully detached background process.

    ``env`` replaces the child's whole environment; ``None`` (the default)
    inherits this process's. Callers that need to hand the child one extra
    variable pass a copy with that variable added, rather than setting it on
    ``os.environ`` here — a spawn must not leave its caller's environment
    changed behind it.
    """
    paths.daemon_dir().mkdir(parents=True, exist_ok=True)
    log = open(paths.log_file(), "ab")
    kwargs = {}
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [sys.executable, "-m", "claude_launcher.daemon"],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            env=env,
            **kwargs,
        )
    finally:
        log.close()


def ensure_running(*, auto_start: bool = True) -> DaemonClient:
    """Connect to the daemon, auto-starting it if needed."""
    client = connect()
    if client is not None:
        return client
    if not auto_start:
        raise DaemonClientError(
            "daemon is not running (start it with 'claunch daemon start')"
        )
    spawn_daemon()
    deadline = time.monotonic() + START_TIMEOUT
    while time.monotonic() < deadline:
        client = connect()
        if client is not None:
            return client
        time.sleep(0.1)
    raise DaemonClientError(
        f"daemon did not come up within {int(START_TIMEOUT)}s "
        f"(see {paths.log_file()})"
    )


def stop(*, timeout: float = 10.0) -> bool:
    """Ask a running daemon to shut down; returns False if none was running."""
    client = connect()
    if client is None:
        return False
    client.post("/api/daemon/shutdown", timeout=5.0)
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        # daemon.json disappears early in shutdown; the singleton lock is only
        # released when the process has fully exited (sessions drained). Wait
        # for the lock too, or an immediate restart's fresh daemon loses the
        # lock race against the dying predecessor and exits.
        if (
            runtime_state.read_daemon_json() is None
            and not _health_ok(client.base_url)
            and runtime_state.lock_is_free()
        ):
            return True
        time.sleep(0.1)
    return True  # request accepted; daemon is still draining sessions


def status() -> Optional[dict]:
    """daemon.json merged with live /api/daemon info, or None when not running."""
    doc = runtime_state.read_daemon_json()
    if not doc:
        return None
    client = connect()
    if client is None:
        return None
    try:
        info = client.get("/api/daemon")
    except DaemonClientError:
        return None
    merged = dict(doc)
    merged.update(info if isinstance(info, dict) else {})
    return merged


# --------------------------------------------------------------------------- #
# diagnosis: telling "not running" apart from "running but not answering"
# --------------------------------------------------------------------------- #
#: States :func:`diagnose` reports.
NOT_RUNNING = "not_running"    # nothing announced, or the announcement is stale
STALE_RECORD = "stale_record"  # daemon.json names a pid that is gone
WEDGED = "wedged"              # the process is alive and holds the lock, but
                               # answers nothing — an event loop that stopped
                               # turning takes the whole HTTP surface with it
SERVING = "serving"            # answering health checks
UNRESPONSIVE = "unresponsive"  # answered nothing inside a SHORT budget while
                               # the process lives -- an observation, not a
                               # verdict: from the outside a merely busy
                               # daemon looks exactly like this


def process_alive(pid: int) -> Optional[bool]:
    """Whether ``pid`` is a live process; ``None`` when we cannot tell.

    Deliberately not ``os.kill(pid, 0)``: on Windows Python maps ``os.kill``
    onto ``TerminateProcess``, so the usual POSIX liveness probe would *kill*
    the daemon it was asked about. Windows gets a query-only handle instead,
    and POSIX keeps signal 0 (``EPERM`` means alive but not ours).
    """
    if pid <= 0:
        return None
    if sys.platform == "win32":
        import ctypes
        from ctypes import wintypes

        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        STILL_ACTIVE = 259
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        handle = kernel32.OpenProcess(
            PROCESS_QUERY_LIMITED_INFORMATION, False, pid
        )
        if not handle:
            return False  # gone (or, rarely, not ours to look at)
        try:
            code = wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
                return None
            return code.value == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    except OSError:
        return None
    return True


def diagnose(*, budget: Optional[float] = None, gap: float = PROBE_GAP) -> dict:
    """What the daemon is doing, from the outside, with the evidence for it.

    Exists because "cannot connect" has two very different causes and the
    client used to report both as "not running": a daemon whose event loop is
    blocked keeps its pid, its port and — the part that makes it unrecoverable
    without help — its singleton lock, so every attempt to start a replacement
    stands down for a predecessor that is never coming back.

    The judgment is budget-based, not count-based. Probes run ``gap`` apart
    until one answers or ``budget`` seconds are spent; a count of consecutive
    failures says how often we looked, not how long the daemon was silent —
    and it was exactly that (2–3 probes, ≈4.5s) that once let this function
    call a daemon wedged while ``ensure_running`` would have sat out the same
    silence as a normal start. So:

    - one answered probe anywhere inside the budget → SERVING;
    - zero answers across a budget of at least ``VERDICT_BUDGET``, from a
      process that still lives → WEDGED, the verdict, with budget, probe
      count and answer count in the report (the probe count is a phase clue
      only: a wedged daemon flips between timing probes out and refusing
      them outright, which swings how many probes fit the same budget);
    - zero answers across anything shorter → UNRESPONSIVE, the same facts
      named as an observation, because no short look can tell busy from
      stuck.

    A dead pid short-circuits to STALE_RECORD after the first failed probe —
    the budget is patience for a live process, and that one is gone.
    """
    verdict_budget = VERDICT_BUDGET
    if budget is None:
        budget = verdict_budget
    doc = runtime_state.read_daemon_json()
    if not doc:
        return {
            "state": NOT_RUNNING,
            "pid": None,
            "started_at": None,
            "base_url": None,
            "lock_free": runtime_state.lock_is_free(),
            "budget": budget,
            "probes": 0,
            "successes": 0,
            "why": "no daemon.json — nothing has announced itself",
        }
    base_url = _base_url(doc)
    pid = int(doc.get("pid") or 0)
    # Carried into every report so an "unconfirmed" answer can name the very
    # process its reader would otherwise go and replace.
    started_at = doc.get("started_at") or None

    def _stale(probes: int) -> dict:
        return {
            "state": STALE_RECORD,
            "pid": pid,
            "started_at": started_at,
            "base_url": base_url,
            "lock_free": runtime_state.lock_is_free(),
            "budget": budget,
            "probes": probes,
            "successes": 0,
            "why": f"daemon.json names pid {pid}, which is gone",
        }

    deadline = time.monotonic() + max(0.0, budget)
    probes = 0
    while True:
        probes += 1
        if _health_ok(base_url):
            return {
                "state": SERVING,
                "pid": pid,
                "started_at": started_at,
                "base_url": base_url,
                "lock_free": False,
                "budget": budget,
                "probes": probes,
                "successes": 1,
                "why": f"answering /api/health (probe {probes})",
            }
        if probes == 1 and process_alive(pid) is False:
            return _stale(probes)
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        time.sleep(min(max(gap, 0.0), remaining))
    if process_alive(pid) is False:
        return _stale(probes)
    if budget >= verdict_budget:
        return {
            "state": WEDGED,
            "pid": pid,
            "started_at": started_at,
            "base_url": base_url,
            # Reported rather than assumed: a wedged daemon normally still
            # holds the lock, and whether it does decides if a replacement
            # can start.
            "lock_free": runtime_state.lock_is_free(),
            "budget": budget,
            "probes": probes,
            "successes": 0,
            "why": (
                f"pid {pid} is alive and {base_url} is announced, yet "
                f"{probes} health probe(s) across {budget:.0f}s got "
                f"0 answers"
            ),
        }
    return {
        "state": UNRESPONSIVE,
        "pid": pid,
        "started_at": started_at,
        "base_url": base_url,
        "lock_free": runtime_state.lock_is_free(),
        "budget": budget,
        "probes": probes,
        "successes": 0,
        "why": (
            f"pid {pid} is alive and {base_url} is announced, but nothing "
            f"answered within {budget:.1f}s ({probes} probe(s)) — an "
            f"observation, not a verdict: a merely busy daemon can look "
            f"exactly like this"
        ),
    }


def terminate_process(pid: int, *, timeout: float = 10.0) -> bool:
    """End ``pid`` and wait for it to actually go; ``True`` when it is gone.

    The last resort for a daemon that stopped answering. There is no gentler
    lever: the singleton lock is an OS file lock held by the process itself,
    so nothing outside it can hand the lock to a successor — the holder has
    to die first. On Windows the whole process tree goes (``taskkill /T``),
    because a daemon killed on its own would leave its session children
    running with nothing driving them, and the successor would then restore
    those same sessions a second time.
    """
    if pid <= 0:
        return False
    try:
        if sys.platform == "win32":
            subprocess.run(
                ["taskkill", "/PID", str(pid), "/T", "/F"],
                capture_output=True,
                timeout=timeout,
            )
        else:
            import signal

            os.kill(pid, signal.SIGTERM)
    except (OSError, subprocess.SubprocessError):
        return False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if process_alive(pid) is False:
            return True
        time.sleep(0.2)
    return process_alive(pid) is False
