"""The daemon's cflow v2 executor: ``session`` children.

``docs/cflow-v2.md`` sections 6.4 and 7. The runtime records a delegation and
leaves it ``queued``; :class:`..cflow2_clock.Cflow2Clock` picks it up and calls
:func:`spawn_session`.

**Session.** A child of the owning session spawned through the daemon's own
``POST /api/sessions/<parent>/children`` (the path the MCP ``spawn`` tool
ends in, so policy, worktree and onboarding are the usual ones), with the
brief as its task, no workflow (``-``) and no board issue. The child submits
with ``flow_delegate(scope=<parent>, cwd=<run dir>)``.

The ``headless`` executor (one non-interactive harness process per attempt)
was removed (claunch-xjbrh); :func:`kill_tree` stays for the clock's probes.
"""

from __future__ import annotations

import contextlib
import hashlib
import logging
import os
import secrets
import signal
import subprocess
import sys
from typing import Any, Callable, Dict, Optional

from ..cflow2 import executors, ir, runtime
from ..cflow2.store import RunStore, unqualified

log = logging.getLogger("claunch.daemon.cflow2")


def kill_tree(pid: int) -> None:
    """End ``pid`` and its descendants. POSIX kills the process group the
    daemon started; Windows has no group id, so ``taskkill /T`` walks the
    tree (a venv ``python.exe`` is a launcher whose child does the work)."""
    pid = int(pid)
    if sys.platform != "win32":
        with contextlib.suppress(OSError, ProcessLookupError):
            os.killpg(pid, signal.SIGKILL)
        return
    try:  # pragma: no cover - windows
        subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"], stdin=subprocess.DEVNULL,
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, timeout=15)
    except (OSError, subprocess.SubprocessError):
        with contextlib.suppress(OSError, ProcessLookupError):
            os.kill(pid, signal.SIGTERM)


# ---------------------------------------------------------------------------
# session


class SpawnRefused(Exception):
    """The daemon would not create the child (policy, a bad request)."""


def default_spawn(parent: str, body: Dict[str, Any]) -> str:
    """Create a child of ``parent`` through the daemon's own spawn route;
    returns the child's name. Blocking: call from a worker thread (it is an
    HTTP call to this same daemon)."""
    from .. import daemon_client

    client = daemon_client.connect()
    if client is None:
        raise SpawnRefused("the daemon API is not reachable")
    try:
        doc = client.post(f"/api/sessions/{parent}/children", body, timeout=120.0)
    except daemon_client.DaemonClientError as exc:
        raise SpawnRefused(str(exc)) from exc
    name = ((doc or {}).get("session") or {}).get("name")
    if not name:
        raise SpawnRefused(f"spawn answered without a session name: {doc!r}")
    return str(name)


def delegation_marker(st: RunStore, run_id: Optional[str], did: str) -> str:
    """The line a ``session`` delegation's spawn task carries, by which a
    child created for it is recognised among the parent's children (a spawn
    whose answer was lost, an orphan after a reroute)."""
    return f"[cflow2-delegation {st.scope}/{run_id or 'run'}/{did}]"


def spawn_body(brief: Dict[str, Any], *, cwd: str, scope: str, receipt_path: str,
               mcp: bool = True, marker: Optional[str] = None,
               receipt_token: Optional[str] = None) -> Dict[str, Any]:
    task = executors.session_task(brief, cwd=cwd, scope=scope,
                                  receipt_path=receipt_path, mcp=mcp,
                                  receipt_token=receipt_token)
    if marker:
        task = f"{task}\n\n{marker}"
    return {
        "task": task,
        # No v1 run for the child (onboard.NO_WORKFLOW) and no board issue:
        # the delegation record is the child's whole assignment.
        "workflow": "-",
        "beads": False,
    }


def receipt_key_hash(token: str) -> str:
    return hashlib.sha256(str(token).encode("utf-8")).hexdigest()


#: ``runner.phase`` of a session delegation between the claim and the
#: child's name coming back. Persisted before the spawn call, so a daemon
#: that dies in between leaves a record the next one recognises.
PHASE_SPAWNING = "spawning"
PHASE_SPAWNED = "spawned"

FindChild = Callable[[str, str], Optional[str]]


def spawn_session(cwd: str, scope: str, did: str, *,
                  spawn: Callable[[str, Dict[str, Any]], str] = default_spawn,
                  find_child: Optional[FindChild] = None,
                  runner: Optional[Dict[str, Any]] = None,
                  lost: Optional[Callable[[str], None]] = None) -> Dict[str, Any]:
    """Start a claimed ``session`` delegation: spawn the child and record it.

    The claim (status ``running``, ``runner.phase: spawning`` with the
    task's marker) is already persisted. A refusal moves the delegation to
    the node's next executor -- unless ``find_child(parent, marker)`` finds
    the child after all (the POST timed out while the daemon went on and
    created it), which is then adopted. When the delegation is rerouted
    after an error that may still produce a child, ``lost(marker)`` is told
    so the clock can end that child if it appears later.
    """
    st = RunStore(cwd, scope)
    r = runtime.Run.load(st)
    rec = r.state["delegations"][did]
    brief = runtime.brief(r, did)
    marker = (runner or {}).get("marker") or delegation_marker(st, r.state.get("run_id"), did)
    mcp = ((rec.get("route") or {}).get("returns") or {}).get("route") != "receipt-file"
    st.receipts_dir.mkdir(parents=True, exist_ok=True)  # the child writes there
    # A child without MCP returns its result as a file. The file must carry
    # a key only the child's task text holds; the record keeps its hash, so
    # a receipt written by anyone else (the parent driver) is refused (G1).
    token = None
    if not mcp and not brief.get("nested"):
        token = secrets.token_hex(16)
        runner = dict(runner or {}, receipt_key=receipt_key_hash(token))
        # Persisted before the spawn, like the claim: a child adopted after
        # a restart still has a key to be checked against.
        runtime.delegate_update(did, cwd=cwd, scope=scope, runner=runner)
    body = spawn_body(brief, cwd=str(st.cwd), scope=st.scope,
                      receipt_path=str(st.receipt_path(did, ".child.json")), mcp=mcp,
                      marker=marker, receipt_token=token)
    # the parent is named as the session manager names it: the slot without
    # its daemon (``s15`` for ``s15@main``, claunch-9jmdz)
    parent = unqualified(st.scope)
    try:
        child = spawn(parent, body)
    except Exception as exc:  # noqa: BLE001 - every refusal reroutes
        found = None
        if find_child is not None:
            with contextlib.suppress(Exception):
                found = find_child(parent, marker)
        if found:
            _record_child(cwd, scope, did, found, runner, marker, adopted=str(exc))
            return {"delegation": did, "result": "adopted", "child": found, "error": str(exc)}
        refused = isinstance(exc, SpawnRefused) and "not reachable" in str(exc)
        if lost is not None and not refused:
            lost(marker)
        nxt = runtime.delegate_reroute(did, f"session.spawn refused: {exc}", cwd=cwd, scope=scope)
        return {"delegation": did, "result": "rerouted", "executor": nxt, "error": str(exc)}
    _record_child(cwd, scope, did, child, runner, marker)
    return {"delegation": did, "result": "spawned", "child": child}


def _record_child(cwd: str, scope: str, did: str, child: str,
                  runner: Optional[Dict[str, Any]], marker: str,
                  adopted: Optional[str] = None) -> bool:
    marker_doc = dict(runner or {})
    marker_doc.update(phase=PHASE_SPAWNED, marker=marker)
    event: Dict[str, Any] = {"event": "delegate_started", "phase": "spawned",
                             "executor": ir.EXEC_SESSION, "child": child}
    if adopted:
        event["adopted"] = f"spawn call failed ({adopted[:200]}) but the child exists"
    # ``assignee`` is who may submit for a session delegation (C2): the
    # child, and nobody else.
    return runtime.delegate_update(did, cwd=cwd, scope=scope, child=child, assignee=child,
                                   runner=marker_doc, event=event)
