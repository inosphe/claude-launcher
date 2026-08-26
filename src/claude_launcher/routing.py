"""Request-body routing for providers that take their routing in the body.

Some backends decide *which upstream compute serves a request* from a field in
the request body rather than from the URL or a header. OpenRouter is the case
this exists for: pinning a provider (say CoreWeave) is
``provider: {"order": ["coreweave"], "allow_fallbacks": false}`` **in the JSON
body**, and nothing else does it — a ``:coreweave`` suffix on the model slug is
accepted and silently ignored (the request lands on whatever endpoint the
default router picks), and ``@coreweave`` is rejected as an invalid model id.

The launcher only ever hands the harness *environment variables*, so it cannot
reach the body. The gap is closed by a small local shim: a loopback HTTP proxy
that forwards to the real upstream and merges the configured spec into every
JSON request body. A provider that declares ``routing`` gets its
``ANTHROPIC_BASE_URL`` rewritten to the shim at launch::

    providers:
      openrouter:
        routing:
          order: [coreweave]
          allow_fallbacks: false
        env:
          ANTHROPIC_BASE_URL: "https://openrouter.ai/api/"
          ...

The spec is passed to the upstream verbatim, so every field the backend
understands (``order``, ``only``, ``ignore``, ``sort``, ``allow_fallbacks``,
``max_price``, ...) works without this module knowing the vocabulary.

One shim serves every session that wants the same (upstream, spec) pair, which
a fingerprint over that pair identifies. Sessions launching at the same moment
converge on one shim through an exclusive *start claim* file: whoever takes it
spawns, the rest wait for what it started. (Watching the port instead is not
enough — between binding and answering there is a window where a shim looks
like a stranger, and the next caller starts a duplicate beside it.) A changed
spec is a different fingerprint, so config edits are picked up by the next
launch instead of being served stale by a running shim.
"""

from __future__ import annotations

import hashlib
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional

from . import config, store

#: Per-provider config key holding the body spec (``providers.<name>.routing``).
SPEC_KEY = "routing"

#: Body field the spec is merged into. OpenRouter's name for it.
BODY_FIELD = "provider"

#: Shim-only endpoints, namespaced so they cannot collide with an upstream path.
HEALTH_PATH = "/__claunch__/routing"
SHUTDOWN_PATH = "/__claunch__/shutdown"

#: Marker every shim health response carries, so a stranger listening on the
#: derived port is never mistaken for one of ours.
MARKER = "claunch-routing-shim"

# Ports are derived from the fingerprint (see module docstring). The window sits
# in the registered range, below the ephemeral ports both Windows and Linux hand
# out for outbound sockets — a shim must not lose its port to a random connect.
_PORT_BASE = 31500
_PORT_SPAN = 4000
_PORT_TRIES = 8

#: How long to wait for a freshly spawned shim to answer its health endpoint.
START_TIMEOUT = 15.0

#: How long to wait for a shim asked to shut down to release its port.
STOP_TIMEOUT = 5.0


class RoutingError(Exception):
    """Raised for a malformed ``routing`` block or a shim that will not start."""


# --------------------------------------------------------------------------- #
# configuration
# --------------------------------------------------------------------------- #
def spec(provider_name: str, doc: Optional[dict] = None) -> Optional[Dict]:
    """The routing spec declared on ``provider_name``, or ``None``."""
    doc = store.load() if doc is None else doc
    section = doc.get("providers")
    if not isinstance(section, dict):
        return None
    entry = section.get(provider_name)
    if not isinstance(entry, dict):
        return None
    block = entry.get(SPEC_KEY)
    if block is None:
        return None
    if not isinstance(block, dict) or not block:
        raise RoutingError(
            f"providers.{provider_name}.{SPEC_KEY} must be a non-empty mapping "
            f"(got {type(block).__name__})"
        )
    return dict(block)


def configured(doc: Optional[dict] = None) -> Dict[str, Dict]:
    """Every provider that declares a routing spec, name -> spec."""
    doc = store.load() if doc is None else doc
    section = doc.get("providers")
    out: Dict[str, Dict] = {}
    if not isinstance(section, dict):
        return out
    for name in section:
        block = spec(str(name), doc)
        if block:
            out[str(name)] = block
    return out


def set_spec(provider_name: str, block: Optional[Dict]) -> None:
    """Write (or, with ``None``, drop) a provider's routing spec."""
    if block is not None and (not isinstance(block, dict) or not block):
        raise RoutingError("a routing spec must be a non-empty mapping")

    def _mutate(doc: dict) -> None:
        section = doc.get("providers")
        if not isinstance(section, dict):
            raise RoutingError(
                f"unknown provider {provider_name!r} (see 'claunch providers')"
            )
        entry = section.get(provider_name)
        if not isinstance(entry, dict):
            raise RoutingError(
                f"unknown provider {provider_name!r} (see 'claunch providers')"
            )
        if block is None:
            entry.pop(SPEC_KEY, None)
        else:
            entry[SPEC_KEY] = dict(block)

    store.update(_mutate)


# --------------------------------------------------------------------------- #
# the pure part: what the shim does to a body
# --------------------------------------------------------------------------- #
def canonical(block: Dict) -> str:
    """Stable JSON for a spec — the fingerprint and the merge both use it."""
    return json.dumps(block, sort_keys=True, separators=(",", ":"))


def fingerprint(upstream: str, block: Dict) -> str:
    """Identity of one shim: the upstream it fronts and the spec it merges."""
    return hashlib.sha256(
        f"{upstream}\n{canonical(block)}".encode("utf-8")
    ).hexdigest()[:16]


def merge_body(raw: bytes, block: Dict) -> bytes:
    """``raw`` with ``block`` merged in as the routing field.

    Anything that is not a JSON object is returned untouched, so the shim stays
    a transparent proxy for uploads, form posts and malformed bodies alike. A
    body that already carries the field is left alone: an explicit choice by
    whoever built the request outranks the launcher's default.
    """
    if not raw:
        return raw
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return raw
    if not isinstance(doc, dict) or BODY_FIELD in doc:
        return raw
    doc[BODY_FIELD] = json.loads(canonical(block))
    return json.dumps(doc).encode("utf-8")


# --------------------------------------------------------------------------- #
# shim discovery and lifecycle
# --------------------------------------------------------------------------- #
def state_dir() -> Path:
    """Where running shims record themselves (per-machine runtime state)."""
    return config.launcher_home() / "routing"


def log_file(fp: str) -> Path:
    return state_dir() / f"{fp}.log"


def _record_file(fp: str) -> Path:
    return state_dir() / f"{fp}.json"


def candidate_ports(fp: str) -> List[int]:
    """The ports a shim with this fingerprint may occupy, in preference order."""
    start = _PORT_BASE + int(fp[:8], 16) % _PORT_SPAN
    return [start + i for i in range(_PORT_TRIES)]


def local_url(port: int) -> str:
    return f"http://127.0.0.1:{port}/"


def health(port: int, timeout: float = 0.6) -> Optional[dict]:
    """Ask whatever listens on ``port`` whether it is a claunch shim."""
    try:
        with urllib.request.urlopen(
            f"http://127.0.0.1:{port}{HEALTH_PATH}", timeout=timeout
        ) as resp:
            info = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, OSError, ValueError):
        return None
    if not isinstance(info, dict) or info.get("marker") != MARKER:
        return None
    return info


def _port_free(port: int) -> bool:
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", port))
        return True
    except OSError:
        return False
    finally:
        sock.close()


def _shim_env() -> dict:
    """Child env that pins the shim to *this* checkout's source tree.

    The launcher frequently runs out of a git worktree while an editable
    install points at the main checkout; ``-m claude_launcher.routing_shim``
    would then load the other tree's code. Prepending this package's parent to
    ``PYTHONPATH`` makes the shim always the sibling of the code that spawned
    it — which is also what lets the test suite exercise a real shim process.
    """
    env = {
        k: v
        for k, v in os.environ.items()
        # The shim proxies whatever credentials the caller sends and needs none
        # of its own; a long-lived process should not hold the token as well.
        if not k.startswith(("ANTHROPIC_", "CLAUDE_CODE_"))
    }
    pkg_parent = str(Path(__file__).resolve().parent.parent)
    parts = [pkg_parent] + [p for p in env.get("PYTHONPATH", "").split(os.pathsep) if p]
    env["PYTHONPATH"] = os.pathsep.join(dict.fromkeys(parts))
    return env


def _spawn(upstream: str, block: Dict, port: int, fp: str) -> None:
    """Start a detached shim process; its output goes to the fingerprint's log."""
    state_dir().mkdir(parents=True, exist_ok=True)
    log = open(log_file(fp), "ab")
    kwargs: dict = {}
    if sys.platform == "win32":
        CREATE_NO_WINDOW = 0x08000000
        CREATE_NEW_PROCESS_GROUP = 0x00000200
        kwargs["creationflags"] = CREATE_NO_WINDOW | CREATE_NEW_PROCESS_GROUP
    else:
        kwargs["start_new_session"] = True
    try:
        subprocess.Popen(
            [
                sys.executable,
                "-m",
                "claude_launcher.routing_shim",
                "--upstream", upstream,
                "--spec", canonical(block),
                "--port", str(port),
                "--fingerprint", fp,
            ],
            stdin=subprocess.DEVNULL,
            stdout=log,
            stderr=log,
            close_fds=True,
            env=_shim_env(),
            **kwargs,
        )
    finally:
        log.close()


def _record(fp: str, port: int, upstream: str, block: Dict, pid) -> None:
    state_dir().mkdir(parents=True, exist_ok=True)
    _record_file(fp).write_text(
        json.dumps(
            {
                "fingerprint": fp,
                "port": port,
                "upstream": upstream,
                "spec": block,
                "pid": pid,
                "url": local_url(port),
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )


def _log_tail(fp: str, lines: int = 12) -> str:
    try:
        text = log_file(fp).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""
    return "\n".join(text.strip().splitlines()[-lines:])


def _claim_file(fp: str) -> Path:
    return state_dir() / f"{fp}.claim"


def _try_claim(fp: str) -> bool:
    """Win the right to *start* this fingerprint's shim, or report that another
    process already holds it.

    Testing the port is not enough on its own: between a shim binding its socket
    and answering its health endpoint there is a window where the port reads as
    busy but no shim answers, and a second caller would take that for a stranger
    and start a duplicate on the next port. An exclusive file makes the start
    itself the thing that is claimed, so exactly one process spawns and the rest
    wait for it.
    """
    state_dir().mkdir(parents=True, exist_ok=True)
    path = _claim_file(fp)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except FileExistsError:
        try:
            age = time.time() - path.stat().st_mtime
        except OSError:  # it went away underneath us — the holder finished
            return _try_claim(fp)
        if age < START_TIMEOUT + 5:
            return False
        # Older than any start could possibly be: the holder died mid-spawn.
        path.unlink(missing_ok=True)
        return _try_claim(fp)
    try:
        os.write(fd, str(os.getpid()).encode("ascii"))
    finally:
        os.close(fd)
    return True


def _find(fp: str) -> Optional[int]:
    """The port a live shim for ``fp`` answers on, if one is up."""
    for port in candidate_ports(fp):
        info = health(port)
        if info is not None and info.get("fingerprint") == fp:
            return port
    return None


def _free_port(fp: str) -> Optional[int]:
    for port in candidate_ports(fp):
        if _port_free(port):
            return port
    return None


def ensure_shim(upstream: str, block: Dict) -> str:
    """Base URL of a live shim fronting ``upstream`` with ``block`` merged in.

    Reuses a running shim for the same pair and starts one otherwise. Callers
    that arrive together converge on a single shim: one of them takes the start
    claim and the others wait for what it starts.
    """
    fp = fingerprint(upstream, block)
    port = _find(fp)
    if port is not None:
        _record(fp, port, upstream, block, None)
        return local_url(port)
    if not _try_claim(fp):
        # Another launch is starting this exact shim right now. Wait for it
        # rather than adding a second one beside it.
        deadline = time.monotonic() + START_TIMEOUT + 5
        while time.monotonic() < deadline:
            port = _find(fp)
            if port is not None:
                _record(fp, port, upstream, block, None)
                return local_url(port)
            time.sleep(0.1)
        raise RoutingError(
            f"another process holds the start claim for the routing shim to "
            f"{upstream} but none came up (stale {_claim_file(fp)}?)"
        )
    try:
        start_port = _free_port(fp)
        if start_port is None:
            raise RoutingError(
                f"no free loopback port for the routing shim in "
                f"{candidate_ports(fp)[0]}..{candidate_ports(fp)[-1]}"
            )
        _spawn(upstream, block, start_port, fp)
        deadline = time.monotonic() + START_TIMEOUT
        while time.monotonic() < deadline:
            info = health(start_port, timeout=0.5)
            if info is not None and info.get("fingerprint") == fp:
                _record(fp, start_port, upstream, block, info.get("pid"))
                return local_url(start_port)
            time.sleep(0.1)
        tail = _log_tail(fp)
        raise RoutingError(
            f"routing shim for {upstream} did not come up on port {start_port} "
            f"within {int(START_TIMEOUT)}s (see {log_file(fp)})"
            + (("\n" + tail) if tail else "")
        )
    finally:
        _claim_file(fp).unlink(missing_ok=True)


def apply(env: dict, provider_name: str, doc: Optional[dict] = None) -> None:
    """Point ``env``'s base URL at a shim when ``provider_name`` wants routing.

    A no-op for providers without a ``routing`` block. When one is declared the
    shim *must* come up: falling back to the direct URL would silently drop the
    pin, which is precisely the failure this feature exists to prevent, so a
    shim that cannot start fails the launch instead.
    """
    block = spec(provider_name, doc)
    if not block:
        return
    upstream = env.get("ANTHROPIC_BASE_URL")
    if not upstream:
        raise RoutingError(
            f"provider {provider_name!r} declares {SPEC_KEY} but sets no "
            "ANTHROPIC_BASE_URL to route to"
        )
    # No "is this already a shim?" guard: ``ANTHROPIC_BASE_URL`` is a backend
    # key, stripped from the inherited environment before any layer is applied
    # (see :data:`runner.BACKEND_ENV_KEYS`), so the value here always comes
    # from the config file. A guard keyed on "looks like loopback" would
    # instead skip the rewrite for a locally hosted upstream — silently
    # dropping the pin, which is the failure this whole module exists to stop.
    env["ANTHROPIC_BASE_URL"] = ensure_shim(upstream, block)


# --------------------------------------------------------------------------- #
# inspection / teardown (`claunch routing`)
# --------------------------------------------------------------------------- #
def instances() -> List[dict]:
    """Every recorded shim that still answers, newest record state cleaned up."""
    out: List[dict] = []
    try:
        records = sorted(state_dir().glob("*.json"))
    except OSError:
        return out
    for rec in records:
        try:
            info = json.loads(rec.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        port = info.get("port")
        live = health(int(port)) if isinstance(port, int) else None
        if live is None or live.get("fingerprint") != info.get("fingerprint"):
            rec.unlink(missing_ok=True)  # stale record: the shim is gone
            continue
        info["pid"] = live.get("pid", info.get("pid"))
        out.append(info)
    return out


def stop(fingerprints: Optional[List[str]] = None) -> List[str]:
    """Ask shims to shut down; returns the fingerprints that were stopped."""
    stopped = []
    for info in instances():
        fp = info.get("fingerprint")
        if fingerprints is not None and fp not in fingerprints:
            continue
        req = urllib.request.Request(
            f"http://127.0.0.1:{info['port']}{SHUTDOWN_PATH}", method="POST", data=b""
        )
        try:
            urllib.request.urlopen(req, timeout=5.0).close()
        except (urllib.error.URLError, OSError):
            pass
        # The shim answers the request and *then* unwinds, so wait for the port
        # to actually go quiet: a caller that immediately relaunches must not
        # find the old process still holding it.
        deadline = time.monotonic() + STOP_TIMEOUT
        while time.monotonic() < deadline and health(int(info["port"])) is not None:
            time.sleep(0.05)
        _record_file(str(fp)).unlink(missing_ok=True)
        stopped.append(str(fp))
    return stopped
