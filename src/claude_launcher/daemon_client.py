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
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            return resp.status == 200
    except Exception:
        return False


def is_serving() -> bool:
    """True when an announced daemon actually answers health checks."""
    doc = runtime_state.read_daemon_json()
    return bool(doc) and _health_ok(_base_url(doc))


def connect() -> Optional[DaemonClient]:
    """A client for the running daemon, or None if it isn't up."""
    doc = runtime_state.read_daemon_json()
    if not doc:
        return None
    base_url = _base_url(doc)
    if not _health_ok(base_url):
        return None
    token = runtime_state.load_or_create_token()
    return DaemonClient(base_url, token)


def spawn_daemon() -> None:
    """Start the daemon as a fully detached background process."""
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
