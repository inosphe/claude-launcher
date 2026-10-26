"""The daemon's cflow v2 executor: ``session`` children.

``docs/cflow-v2.md`` sections 6.4 and 7. The runtime records a delegation and
leaves it ``queued``; :class:`..cflow2_clock.Cflow2Clock` picks it up and calls
:func:`spawn_session`.

**Session.** A child of the owning session spawned through the daemon's own
``POST /api/sessions/<parent>/children`` (the path the MCP ``spawn`` tool
ends in, so policy, worktree and onboarding are the usual ones), with the
brief as its task, no workflow (``-``) and no board issue. The child submits
with ``flow_delegate(scope=<parent>, cwd=<run dir>)``.

**Fork.** A quick-fork of the run's driver (``POST
/api/sessions/<driver>/quick-fork``, the route the operator's press and
``deep_think`` take): the child starts with the driver's whole conversation
and stands in the driver's checkout. It may merge on its own
(``self_merge_allowed``), which is how it ends once its result is in. The
clock refuses a fork (:func:`fork_refusal`) when the driver is itself a
quick-fork, or when fewer than :data:`FORK_MIN_CONTEXT` tokens are left
before the driver's auto-compact -- the copy would start that close to it
too -- and the delegation moves to the node's next executor
(claunch-dm09i.1).

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
from typing import Any, Callable, Dict, Optional, Tuple

from ..cflow2 import executors, ir, runtime
from ..cflow2.store import RunStore, unqualified
from . import actors

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
    # The run's machinery spawns this child, not the parent agent: say so,
    # or the record would name the session the daemon happens to run under.
    client.actor = actors.header_value(actors.CFLOW, parent)
    try:
        doc = client.post(f"/api/sessions/{parent}/children", body, timeout=120.0)
    except daemon_client.DaemonClientError as exc:
        raise SpawnRefused(str(exc)) from exc
    name = ((doc or {}).get("session") or {}).get("name")
    if not name:
        raise SpawnRefused(f"spawn answered without a session name: {doc!r}")
    return str(name)


def default_quick_fork(parent: str, body: Dict[str, Any]) -> str:
    """Quick-fork ``parent`` through the daemon's own route; returns the
    copy's name. Blocking, like :func:`default_spawn`."""
    from .. import daemon_client

    client = daemon_client.connect()
    if client is None:
        raise SpawnRefused("the daemon API is not reachable")
    client.actor = actors.header_value(actors.CFLOW, parent)
    try:
        doc = client.post(f"/api/sessions/{parent}/quick-fork", body, timeout=120.0)
    except daemon_client.DaemonClientError as exc:
        raise SpawnRefused(str(exc)) from exc
    name = ((doc or {}).get("session") or {}).get("name")
    if not name:
        raise SpawnRefused(f"quick-fork answered without a session name: {doc!r}")
    return str(name)


#: Tokens that must be left before the driver's auto-compact for a ``fork``
#: delegation to start (claunch-dm09i, user decision qa-7abc7113). The copy
#: begins with the driver's whole conversation, so it would be that close to
#: compacting too. Daemon setting ``cflow2_fork_min_context`` overrides it.
FORK_MIN_CONTEXT = 100_000


def context_left(session: Any) -> Tuple[Optional[int], str]:
    """``(tokens left before auto-compact, how it was read)`` for a live
    session: the auto-compact threshold (``compact_window``), else the
    model's context window, minus the latest request's context size.
    ``None`` when either number is not known."""
    from . import ctxsize

    sdef = getattr(session, "sdef", None)
    try:
        reading = ctxsize.context_of(sdef, ctxsize.for_session(sdef))
    except Exception as exc:  # noqa: BLE001 - not known is the answer
        return None, f"context reading failed: {exc}"
    if not reading or not reading.get("tokens"):
        return None, "no context reading (no request in the transcript yet)"
    used = int(reading["tokens"])
    window = reading.get("compact_window")
    what = "auto-compact threshold"
    if not window:
        window = reading.get("model_context_window")
        what = "model context window"
    if not window:
        return None, "neither an auto-compact threshold nor a context window is known"
    return int(window) - used, f"{what} {int(window)} - context {used}"


def fork_refusal(session: Any, *, min_left: int = FORK_MIN_CONTEXT,
                 left: Optional[Callable[[Any], Tuple[Optional[int], str]]] = None) -> str:
    """Why ``session`` (the run's driver) may not be quick-forked for a
    ``fork`` delegation now, or "" when it may."""
    sdef = getattr(session, "sdef", None)
    if getattr(sdef, "quick_fork_of", None):
        return (f"the driver {getattr(sdef, 'name', '?')!r} is itself a quick-fork "
                f"(of {sdef.quick_fork_of!r}); a fork does not fork again")
    tokens, how = (left or context_left)(session)
    if tokens is None:
        return f"context left before auto-compact is not known ({how})"
    if tokens < int(min_left):
        return (f"{tokens} tokens left before the driver's auto-compact ({how}), "
                f"below the {int(min_left)} a fork needs")
    return ""


def fork_body(brief: Dict[str, Any], *, cwd: str, scope: str, driver: str,
              marker: Optional[str] = None) -> Dict[str, Any]:
    """The quick-fork request of a ``fork`` delegation: its task under the
    marker block, the delegation marker (by which the clock finds a copy
    whose answer was lost), and the copy's leave to end itself."""
    task = executors.fork_task(brief, cwd=cwd, scope=scope, driver=driver)
    if marker:
        task = f"{task}\n\n{marker}"
    return {"task": task, "self_merge_allowed": True}


def delegation_marker(st: RunStore, run_id: Optional[str], did: str) -> str:
    """The line a ``session`` delegation's spawn task carries, by which a
    child created for it is recognised among the parent's children (a spawn
    whose answer was lost, an orphan after a reroute)."""
    return f"[cflow2-delegation {st.scope}/{run_id or 'run'}/{did}]"


def spawn_body(brief: Dict[str, Any], *, cwd: str, scope: str, receipt_path: str,
               mcp: bool = True, marker: Optional[str] = None,
               receipt_token: Optional[str] = None, workflow: Optional[str] = None,
               inputs: Optional[Dict[str, Any]] = None,
               role: Optional[str] = None) -> Dict[str, Any]:
    task = executors.session_task(brief, cwd=cwd, scope=scope,
                                  receipt_path=receipt_path, mcp=mcp,
                                  receipt_token=receipt_token, workflow=workflow)
    if marker:
        task = f"{task}\n\n{marker}"
    body: Dict[str, Any] = {
        "task": task,
        # No board issue: the delegation record is the child's whole
        # assignment. No run either (onboard.NO_WORKFLOW) unless the node
        # names one (``session_workflow``, claunch-pbh7k): a child driving a
        # v2 run of its own is ended by the daemon when that run is done.
        "workflow": workflow or "-",
        "beads": False,
    }
    if workflow:
        body["workflow_version"] = 2
        if inputs:
            body["cflow_inputs"] = dict(inputs)
    if role:
        # The child's mesh role, when the run it is started on volunteers one
        # (``default_role``). Without it the join infers from the handle, and
        # a delegation child's handle is an ``sN`` — so it silently lands in
        # the vocabulary's default role, which by then says nothing about
        # what this child is for. It rides only with a workflow: a child
        # without a run has no declaration to read a role off.
        body["role"] = str(role).strip()
    return body


#: Start inputs a ``session_workflow`` may declare to learn which delegation
#: it serves; only the ones its chart declares are sent (an undeclared input
#: is refused by the start).
DELEGATION_INPUTS = ("delegation", "delegation_scope", "delegation_cwd")


def session_workflow_inputs(workflow: str, cwd: str, values: Dict[str, Any]) -> Dict[str, Any]:
    """``values`` narrowed to the inputs ``workflow`` declares. A workflow
    that does not compile gets none: the spawn then fails on the same error
    and the delegation reroutes, which is where it is reported."""
    from . import cflow2_web

    try:
        chart = cflow2_web.load(workflow, cwd)
    except Exception:  # noqa: BLE001 - reported by the spawn that follows
        return {}
    declared = (chart.meta or {}).get("inputs") or {}
    return {k: v for k, v in values.items() if k in declared}


def session_workflow_role(workflow: str, cwd: str) -> Optional[str]:
    """The mesh role ``workflow`` volunteers for a child started on it, or
    ``None`` when it volunteers none.

    The declaration is the chart's own ``default_role`` — the same key a
    picker reads to auto-select a workflow for a role, read here in the
    other direction: a run that says "I am the oneshot job" also says which
    role its driver is. Keeping it in the workflow rather than in this
    module means the name exists in the documentation the role set already
    owns (``mesh_roles.DEFAULT_YAML``) and in one YAML line, not as a
    constant in the executor that would have to be edited for every new
    ``session_workflow``.

    An unknown name is *not* filtered here: the join refuses it and the
    spawn reroutes, which is where a bad declaration should surface. A
    workflow that does not compile yields ``None`` for the same reason it
    yields no inputs — the spawn that follows reports it.
    """
    from . import cflow2_web

    try:
        chart = cflow2_web.load(workflow, cwd)
    except Exception:  # noqa: BLE001 - reported by the spawn that follows
        return None
    role = (chart.meta or {}).get("default_role")
    return str(role).strip() or None if role else None


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
                  lost: Optional[Callable[[str], None]] = None,
                  fork: Callable[[str, Dict[str, Any]], str] = default_quick_fork,
                  fork_parent: Optional[str] = None) -> Dict[str, Any]:
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
    if rec.get("executor") == ir.EXEC_FORK:
        return _spawn_fork(st, r, did, brief, marker, runner, fork=fork,
                           find_child=find_child, lost=lost, parent=fork_parent)
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
    workflow = None if brief.get("nested") else r.chart.node(rec["node"]).get("session_workflow")
    inputs = None
    role = None
    if workflow:
        inputs = session_workflow_inputs(workflow, str(st.cwd), {
            "delegation": did, "delegation_scope": st.scope, "delegation_cwd": str(st.cwd)})
        role = session_workflow_role(workflow, str(st.cwd))
    body = spawn_body(brief, cwd=str(st.cwd), scope=st.scope,
                      receipt_path=str(st.receipt_path(did, ".child.json")), mcp=mcp,
                      marker=marker, receipt_token=token, workflow=workflow, inputs=inputs,
                      role=role)
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


def _spawn_fork(st: RunStore, r: Any, did: str, brief: Dict[str, Any], marker: str,
                runner: Optional[Dict[str, Any]], *,
                fork: Callable[[str, Dict[str, Any]], str],
                find_child: Optional[FindChild], lost: Optional[Callable[[str], None]],
                parent: Optional[str]) -> Dict[str, Any]:
    """The ``fork`` half of :func:`spawn_session`: quick-fork the driver
    (``parent``, else the slot's session) with :func:`fork_body`."""
    cwd, scope = str(st.cwd), st.scope
    parent = parent or runtime.driver_of(r.state) or unqualified(scope)
    body = fork_body(brief, cwd=cwd, scope=scope, driver=parent, marker=marker)
    try:
        child = fork(parent, body)
    except Exception as exc:  # noqa: BLE001 - every refusal reroutes
        found = None
        if find_child is not None:
            with contextlib.suppress(Exception):
                found = find_child(parent, marker)
        if found:
            _record_child(cwd, scope, did, found, runner, marker, adopted=str(exc),
                          executor=ir.EXEC_FORK)
            return {"delegation": did, "result": "adopted", "child": found, "error": str(exc)}
        refused = isinstance(exc, SpawnRefused) and "not reachable" in str(exc)
        if lost is not None and not refused:
            lost(marker)
        nxt = runtime.delegate_reroute(did, f"quick-fork refused: {exc}", cwd=cwd, scope=scope)
        return {"delegation": did, "result": "rerouted", "executor": nxt, "error": str(exc)}
    _record_child(cwd, scope, did, child, runner, marker, executor=ir.EXEC_FORK)
    return {"delegation": did, "result": "spawned", "child": child}


def _record_child(cwd: str, scope: str, did: str, child: str,
                  runner: Optional[Dict[str, Any]], marker: str,
                  adopted: Optional[str] = None,
                  executor: str = ir.EXEC_SESSION) -> bool:
    marker_doc = dict(runner or {})
    marker_doc.update(phase=PHASE_SPAWNED, marker=marker)
    event: Dict[str, Any] = {"event": "delegate_started", "phase": "spawned",
                             "executor": executor, "child": child}
    if adopted:
        event["adopted"] = f"spawn call failed ({adopted[:200]}) but the child exists"
    # ``assignee`` is who may submit for a session delegation (C2): the
    # child, and nobody else.
    return runtime.delegate_update(did, cwd=cwd, scope=scope, child=child, assignee=child,
                                   runner=marker_doc, event=event)
