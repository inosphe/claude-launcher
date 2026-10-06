"""The daemon's cflow v2 executors: ``headless`` processes and ``session`` children.

``docs/cflow-v2.md`` sections 6.4 and 7. The runtime records a delegation and
leaves it ``queued``; :class:`..cflow2_clock.Cflow2Clock` picks it up and calls
:func:`run_headless` (in a worker thread) or :func:`spawn_session`.

**Headless.** One non-interactive harness process per attempt, started with
the *owning session's* profile environment (credentials, provider endpoint,
``CLAUDE_CONFIG_DIR`` or the harness home) -- the same environment
:func:`.harness.build_command` gives that session's own terminal -- in the
run's directory (or a throwaway worktree). The argv is assembled from the
run's capability map (:func:`..cflow2.executors.build_argv`), stdout/stderr
go to ``receipts/<id>.out``/``.err`` (``<id>.2.out``/``.err`` for the re-run),
and ``budget.seconds`` (default :data:`DEFAULT_SECONDS`) is a wall clock over
both attempts: past it the process group is killed and the node finishes
``timeout``. A result that cannot be extracted or does not validate is
refused once (the re-run gets the problems), and the second failure fails
the delegation -- the same counting as an agent's ``flow_delegate``, through
the same runtime calls, so the journal reads the same whoever ran it.

**Session.** A child of the owning session spawned through the daemon's own
``POST /api/sessions/<parent>/children`` (the path the MCP ``spawn`` tool
ends in, so policy, worktree and onboarding are the usual ones), with the
brief as its task, no workflow (``-``) and no board issue. The child submits
with ``flow_delegate(scope=<parent>, cwd=<run dir>)``.
"""

from __future__ import annotations

import contextlib
import hashlib
import json
import logging
import os
import secrets
import signal
import subprocess
import sys
import threading
import time
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ..cflow2 import executors, ir, runtime
from ..cflow2.store import RunStore

log = logging.getLogger("claunch.daemon.cflow2")

#: Wall clock for a headless delegation without ``budget.seconds``.
DEFAULT_SECONDS = 1800.0

READONLY_SANDBOXED = ("sandbox-readonly", "throwaway-worktree")


@dataclass
class Launch:
    """How to start the owning session's harness non-interactively."""

    harness: str
    command: List[str]
    env: Dict[str, str]
    #: Model/effort selection args the session runs with.
    extra_args: List[str] = field(default_factory=list)
    #: Where throwaway worktrees go (the session's scratch dir).
    scratch: Optional[str] = None


def launch_for_session(session: Any) -> Launch:
    """The :class:`Launch` for a live session: its harness executable and
    the environment its own terminal was given (no conversation, no
    identity prompt, no opening -- a fresh one-shot process)."""
    from .. import config as launcher_config
    from .. import harnesses as harness_registry
    from . import harness as harness_mod

    sdef = session.sdef
    bare = replace(sdef, args=(), identity=None, resume=None, fork_session=False,
                   conversation_id=None, disable_artifact_tool=False)
    _argv, env, _cwd = harness_mod.build_command(bare)
    entry = harness_registry.get(sdef.harness)
    extra: List[str] = []
    if sdef.harness == harness_mod.CLAUDE_HARNESS:
        command = [launcher_config.claude_bin()]
        if sdef.model:
            model_id = entry.model_aliases.get(sdef.model, sdef.model) if entry else sdef.model
            extra.append(f"--model={model_id}")
    else:
        if entry is None:
            raise RuntimeError(f"unknown harness {sdef.harness!r}")
        command = list(entry.launch_command())
        model_id = entry.model_aliases.get(sdef.model, sdef.model) or ""
        if sdef.model:
            extra += [str(a).replace("{model}", model_id) for a in entry.model_args] \
                or [f"--model={model_id}"]
        if sdef.effort and entry.effort_args:
            extra += [str(a).replace("{effort}", str(sdef.effort)) for a in entry.effort_args]
    return Launch(harness=sdef.harness, command=command, env=dict(env), extra_args=extra,
                  scratch=env.get("CLAUNCH_SCRATCH"))


# ---------------------------------------------------------------------------
# processes


class Procs:
    """Live headless processes, keyed by the job that started them, so the
    clock can end one (its run finished, was aborted or halted, or the
    delegation was cancelled) and a daemon shutdown can end them all.

    Each kill records *why* (:data:`KILL_SHUTDOWN`, :data:`KILL_HALT`,
    :data:`KILL_CANCELLED`); the worker reads it with :meth:`reason` after
    its process exits and does not take the exit for a failed attempt. A
    shutdown also sets :attr:`stopping`, after which no worker starts
    another attempt.
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: Dict[Any, subprocess.Popen] = {}
        self._killed: Dict[Any, str] = {}
        self.stopping = threading.Event()

    def add(self, proc: subprocess.Popen, key: Any = None) -> None:
        with self._lock:
            self._procs[key if key is not None else proc.pid] = proc

    def remove(self, proc: subprocess.Popen, key: Any = None) -> None:
        with self._lock:
            k = key if key is not None else proc.pid
            if self._procs.get(k) is proc:
                del self._procs[k]

    def keys(self) -> List[Any]:
        with self._lock:
            return list(self._procs)

    def kill(self, key: Any, why: str) -> bool:
        """End the process group of ``key``'s job; False when none runs."""
        with self._lock:
            proc = self._procs.get(key)
            if proc is not None:
                self._killed.setdefault(key, why)
        if proc is None:
            return False
        _kill(proc)
        return True

    def mark(self, key: Any, why: str) -> None:
        """Record a kill reason for a job between attempts (no process)."""
        with self._lock:
            self._killed.setdefault(key, why)

    def reason(self, key: Any) -> Optional[str]:
        with self._lock:
            why = self._killed.get(key)
        if why is None and self.stopping.is_set():
            return KILL_SHUTDOWN
        return why

    def forget(self, key: Any) -> None:
        with self._lock:
            self._killed.pop(key, None)

    def kill_all(self) -> None:
        self.stopping.set()
        with self._lock:
            items = list(self._procs.items())
            for key, _proc in items:
                self._killed.setdefault(key, KILL_SHUTDOWN)
        for _key, proc in items:
            _kill(proc)


#: Why the clock ended a headless process (:meth:`Procs.kill`).
KILL_SHUTDOWN = "shutdown"   # the daemon is stopping: requeue, attempt unchanged
KILL_HALT = "halt"           # a halt monitor stopped the run: requeue for after release
KILL_CANCELLED = "cancelled"  # run finished/aborted, delegation cancelled: drop it
REQUEUE_KILLS = (KILL_SHUTDOWN, KILL_HALT)


def _kill(proc: subprocess.Popen) -> None:
    try:
        if sys.platform != "win32":
            os.killpg(proc.pid, signal.SIGKILL)
        else:
            proc.kill()
    except (OSError, ProcessLookupError):
        try:
            proc.kill()
        except OSError:
            pass


def process_alive(pid: Optional[int], argv0: Optional[str] = None) -> Optional[bool]:
    """Whether ``pid`` still runs the process a runner marker recorded:
    True/False, or None when it is alive but cannot be identified (another
    program may have the pid). On Linux ``/proc/<pid>/cmdline`` must name
    ``argv0``'s basename; elsewhere a live pid is unidentifiable."""
    from .window import pid_alive

    try:
        pid = int(pid or 0)
    except (TypeError, ValueError):
        return False
    if pid <= 0 or not pid_alive(pid):
        return False
    if not argv0:
        return None
    try:
        raw = Path(f"/proc/{pid}/cmdline").read_bytes()
    except OSError:
        return None
    parts = [p.decode("utf-8", "replace") for p in raw.split(b"\0") if p]
    base = os.path.basename(str(argv0))
    return any(os.path.basename(p) == base for p in parts)


def kill_group(pid: int) -> None:
    """End a process group the daemon started (``start_new_session``: the
    group id is the leader's pid)."""
    try:
        if sys.platform != "win32":
            os.killpg(int(pid), signal.SIGKILL)
        else:  # pragma: no cover - windows
            os.kill(int(pid), signal.SIGTERM)
    except (OSError, ProcessLookupError):
        pass


def run_process(argv: List[str], *, env: Dict[str, str], cwd: str, stdin: Optional[str],
                out_path: Path, err_path: Path, timeout: float,
                procs: Optional[Procs] = None, key: Any = None,
                on_start: Optional[Callable[[subprocess.Popen], None]] = None,
                ) -> Tuple[Optional[int], bool]:
    """Run one attempt: ``(exit code, timed out)``. Output goes to files.
    ``on_start(proc)`` runs right after the process exists (the caller
    persists its pid); an exception there kills it and propagates."""
    out_path.parent.mkdir(parents=True, exist_ok=True)
    kwargs: Dict[str, Any] = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    else:  # pragma: no cover - windows
        kwargs["creationflags"] = getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
    with open(out_path, "wb") as out, open(err_path, "wb") as err:
        proc = subprocess.Popen(
            argv, cwd=cwd, env=env, stdout=out, stderr=err,
            stdin=subprocess.PIPE if stdin is not None else subprocess.DEVNULL, **kwargs,
        )
        if procs is not None:
            procs.add(proc, key)
        try:
            if on_start is not None:
                try:
                    on_start(proc)
                except BaseException:
                    _kill(proc)
                    with contextlib.suppress(subprocess.TimeoutExpired):
                        proc.wait(timeout=10)
                    raise
            if procs is not None and procs.reason(key) is not None:
                _kill(proc)  # killed (or shutdown began) before it was registered
            if stdin is not None:
                try:
                    proc.stdin.write(stdin.encode("utf-8"))  # type: ignore[union-attr]
                    proc.stdin.close()  # type: ignore[union-attr]
                except OSError:
                    pass
            try:
                return proc.wait(timeout=max(0.0, timeout)), False
            except subprocess.TimeoutExpired:
                _kill(proc)
                try:
                    proc.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                return None, True
        finally:
            if procs is not None:
                procs.remove(proc, key)


def git(cwd: str, *args: str, timeout: float = 120.0) -> Tuple[int, str]:
    try:
        proc = subprocess.run(["git", "-C", cwd, *args], capture_output=True, text=True,
                              timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return 1, str(exc)
    return proc.returncode, (proc.stdout or "") + (proc.stderr or "")


def porcelain(cwd: str) -> Optional[str]:
    code, out = git(cwd, "status", "--porcelain")
    return out if code == 0 else None


# ---------------------------------------------------------------------------
# headless


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _requeue(cwd: str, scope: str, did: str, rec: Dict[str, Any], why: str) -> bool:
    """Put an interrupted headless delegation back to ``queued`` with its
    attempt count unchanged (the interruption is not the delegate's fault)."""
    return runtime.delegate_update(
        did, expect=("running", "queued"), cwd=cwd, scope=scope, status="queued",
        runner=None, attempt_running=None,
        event={"event": "delegate_interrupted", "executor": ir.EXEC_HEADLESS,
               "why": why, "attempts": int(rec.get("attempts") or 0)},
    )


def _readonly_changed(before: Optional[str], after: Optional[str]) -> Optional[str]:
    """The porcelain lines a read-only delegation added or removed, or None."""
    if before is None or after is None or before == after:
        return None
    b, a = set(before.splitlines()), set(after.splitlines())
    lines = sorted((a - b) | (b - a))
    return "; ".join(lines[:20]) or "git status --porcelain changed"


def run_headless(cwd: str, scope: str, did: str, launch: Launch, *,
                 default_seconds: float = DEFAULT_SECONDS,
                 procs: Optional[Procs] = None, key: Any = None,
                 runner: Optional[Dict[str, Any]] = None,
                 clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    """Run a claimed headless delegation to its end; returns a summary
    ``{delegation, result: returned|failed|timeout|retry-exhausted|
    interrupted|cancelled|readonly-violation|rerouted|error, ...}``.
    Everything the run should know goes through the runtime (journal + state).

    ``runner`` is the claim's marker; each attempt rewrites it with the
    process's ``pid``, ``argv0`` and ``attempt`` before waiting on it, so a
    later daemon can tell whether that process is still around. A process
    the clock killed (:meth:`Procs.kill`) is not a failed attempt: on a
    shutdown or a halt the delegation goes back to ``queued`` with its
    attempt count unchanged; on a cancel nothing more is written.
    """
    st = RunStore(cwd, scope)
    r = runtime.Run.load(st)
    rec = r.state["delegations"][did]
    summary: Dict[str, Any] = {"delegation": did, "scope": scope, "cwd": str(st.cwd)}
    if rec.get("status") not in runtime.OPEN_DELEGATION or r.status not in runtime.LIVE_STATUSES:
        summary.update(result="error", error=f"delegation {did} is {rec.get('status')} "
                       f"in a {r.status} run")
        return summary
    brief = runtime.brief(r, did)
    plan = rec.get("route") or {}
    caps = r.caps
    returns = rec.get("returns") or {ir.OUTCOME_OK: {}}
    budget = plan.get("budget") or {}
    seconds = float(budget.get("seconds") or default_seconds)
    deadline = clock() + seconds
    readonly = (plan.get("readonly") or {}).get("route")
    returns_route = (plan.get("returns") or {}).get("route")
    run_cwd = str(st.cwd)
    workdir = run_cwd
    worktree: Optional[Path] = None
    # A re-run after a daemon restart continues the count: the refused
    # attempt before it is not handed a fresh pair.
    done_attempts = int(rec.get("attempts") or 0)
    prev_errors = rec.get("errors") or []
    errors: Optional[List[str]] = list(prev_errors[-1]) if done_attempts and prev_errors else None

    def interrupted() -> Optional[str]:
        return procs.reason(key) if procs is not None else None

    if readonly == "throwaway-worktree":
        code, tip = git(run_cwd, "rev-parse", "HEAD")
        root = Path(launch.scratch) if launch.scratch else st.dir / "work"
        worktree = root / "cflow2-work" / f"{r.state.get('run_id') or 'run'}-{did}"
        worktree.parent.mkdir(parents=True, exist_ok=True)
        if code == 0:
            code, out = git(run_cwd, "worktree", "add", "--detach", str(worktree), tip.strip())
        else:
            out = tip
        if code != 0:
            # Nothing would keep the delegate from writing to the run's own
            # checkout: do not run it here at all (A4).
            try:
                nxt = runtime.delegate_reroute(
                    did, f"read-only headless delegation: throwaway worktree failed "
                    f"({out.strip()[:300]}) and no sandbox or tool restriction is "
                    "available", cwd=cwd, scope=scope)
            except runtime.FlowError as exc:
                summary.update(result="error", error=str(exc))
                return summary
            summary.update(result="rerouted", executor=nxt)
            return summary
        workdir = str(worktree)
    # Without a sandbox or a worktree the checkout is the delegate's, so a
    # change to it is a broken read-only promise and fails the delegation.
    check_porcelain = bool(readonly) and readonly not in READONLY_SANDBOXED
    before = porcelain(run_cwd) if check_porcelain else None

    schema = executors.json_schema(returns)
    schema_file: Optional[str] = None
    if returns_route == "schema-flag":
        schema_file = str(st.write_receipt(did, json.dumps(schema, indent=1), ".schema.json"))
    json_output = bool(executors._cap(caps, "headless.output.json").enabled)

    try:
        for attempt in range(done_attempts + 1, 3):
            why = interrupted()
            if why is not None:
                return _stopped(cwd, scope, did, rec, why, summary)
            prompt = executors.build_prompt(brief, errors=errors, readonly=bool(readonly))
            argv, stdin = executors.build_argv(
                launch.command, caps, plan, prompt, harness=launch.harness, schema=schema,
                schema_file=schema_file, extra_args=launch.extra_args,
            )
            suffix = "" if attempt == 1 else f".{attempt}"
            out_path = st.receipt_path(did, f"{suffix}.out")
            err_path = st.receipt_path(did, f"{suffix}.err")

            def started(proc: subprocess.Popen, attempt: int = attempt,
                        out_path: Path = out_path) -> None:
                marker = dict(runner or {})
                marker.update(pid=proc.pid, argv0=str(argv[0]), attempt=attempt,
                              started_at=clock())
                runtime.delegate_update(did, cwd=cwd, scope=scope, attempt_running=attempt,
                                        stdout=str(out_path), runner=marker)

            code, timed_out = run_process(
                argv, env=launch.env, cwd=workdir, stdin=stdin, out_path=out_path,
                err_path=err_path, timeout=deadline - clock(), procs=procs, key=key,
                on_start=started,
            )
            why = interrupted()
            if why is not None:
                return _stopped(cwd, scope, did, rec, why, summary)
            if check_porcelain:
                changed = _readonly_changed(before, porcelain(run_cwd))
                if changed:
                    runtime.note("monitor_violation", cwd=cwd, scope=scope, kind="readonly",
                                 delegation=did, node=rec["node"],
                                 detail="git status --porcelain changed during a read-only "
                                        f"headless delegation: {changed}")
                    runtime.delegate_fail(
                        did, f"read-only delegation changed the working tree ({changed}); "
                        "its result is not used", cwd=cwd, scope=scope,
                        detail={"readonly_violation": changed})
                    summary.update(result="readonly-violation", readonly_violation=True,
                                   changed=changed)
                    return summary
            if timed_out:
                runtime.delegate_fail(did, f"budget.seconds {seconds:g} ran out; the "
                                      f"headless process was killed", outcome=ir.OUTCOME_TIMEOUT,
                                      detail={"seconds": seconds, "stdout": str(out_path)},
                                      cwd=cwd, scope=scope)
                summary["result"] = "timeout"
                return summary
            stdout = _read(out_path)
            got, how = executors.extract_result(stdout, json_output=json_output)
            problems: List[str] = []
            if got is None:
                problems.append(how)
                if code:
                    tail = _read(err_path).strip()[-400:]
                    problems.append(f"the process exited {code}" + (f": {tail}" if tail else ""))
            else:
                if returns_route == "schema-flag" and how == "fenced-block":
                    runtime.note("fallback", cwd=cwd, scope=scope, delegation=did,
                                 node=rec["node"], cause="schema-flag output was not a JSON "
                                 "document", route="schema-flag -> fenced-block",
                                 decision="returns")
                problems = runtime.validate_result(returns, got[0], got[1])
            if not problems:
                _submit(did, got[0], got[1], cwd=cwd, scope=scope)
                summary.update(result="returned", outcome=got[0], attempts=attempt)
                return summary
            retry = runtime.delegate_reject(did, problems, cwd=cwd, scope=scope)
            errors = problems
            if not retry:
                summary.update(result="failed", errors=problems, attempts=attempt)
                return summary
        summary["result"] = "retry-exhausted"
        return summary
    except runtime.FlowError as exc:
        # The run moved on under us (aborted, finished): nothing to submit to.
        summary.update(result="error", error=str(exc))
        return summary
    finally:
        if procs is not None:
            procs.forget(key)
        if worktree is not None:
            code, out = git(run_cwd, "worktree", "remove", "--force", str(worktree))
            if code != 0:
                log.warning("could not remove throwaway worktree %s: %s", worktree, out.strip())


def _stopped(cwd: str, scope: str, did: str, rec: Dict[str, Any], why: str,
             summary: Dict[str, Any]) -> Dict[str, Any]:
    if why in REQUEUE_KILLS:
        try:
            _requeue(cwd, scope, did, rec, why)
        except Exception:  # noqa: BLE001 - shutting down: the next boot requeues it
            log.exception("could not requeue interrupted delegation %s", did)
        summary.update(result="interrupted", why=why)
    else:
        summary.update(result="cancelled", why=why)
    return summary


def _submit(did: str, outcome: str, value: Any, *, cwd: str, scope: str) -> None:
    """Hand a headless result to the runtime as the daemon (not as an agent).

    Uses ``runtime.executor_submit`` when the runtime has it (the
    daemon-internal entry agent A adds, which MCP cannot reach); otherwise
    the public :func:`runtime.delegate_submit`."""
    fn = getattr(runtime, "executor_submit", None)
    if callable(fn):
        fn(did, outcome, value, cwd=cwd, scope=scope)
    else:
        runtime.delegate_submit(did, outcome, value, cwd=cwd, scope=scope)


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
    try:
        child = spawn(scope, body)
    except Exception as exc:  # noqa: BLE001 - every refusal reroutes
        found = None
        if find_child is not None:
            with contextlib.suppress(Exception):
                found = find_child(scope, marker)
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
