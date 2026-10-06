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

import json
import logging
import os
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
    """Live headless processes, so a daemon shutdown can kill them (a
    restart requeues their delegations; an orphan would run twice)."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._procs: Dict[int, subprocess.Popen] = {}

    def add(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs[proc.pid] = proc

    def remove(self, proc: subprocess.Popen) -> None:
        with self._lock:
            self._procs.pop(proc.pid, None)

    def kill_all(self) -> None:
        with self._lock:
            procs = list(self._procs.values())
        for proc in procs:
            _kill(proc)


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


def run_process(argv: List[str], *, env: Dict[str, str], cwd: str, stdin: Optional[str],
                out_path: Path, err_path: Path, timeout: float,
                procs: Optional[Procs] = None) -> Tuple[Optional[int], bool]:
    """Run one attempt: ``(exit code, timed out)``. Output goes to files."""
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
            procs.add(proc)
        try:
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
                procs.remove(proc)


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


def run_headless(cwd: str, scope: str, did: str, launch: Launch, *,
                 default_seconds: float = DEFAULT_SECONDS,
                 procs: Optional[Procs] = None,
                 clock: Callable[[], float] = time.time) -> Dict[str, Any]:
    """Run a claimed headless delegation to its end; returns a summary
    ``{delegation, result: returned|failed|timeout|retry-exhausted|error, ...}``.
    Everything the run should know goes through the runtime (journal + state).
    """
    st = RunStore(cwd, scope)
    r = runtime.Run.load(st)
    rec = r.state["delegations"][did]
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
    summary: Dict[str, Any] = {"delegation": did, "scope": scope, "cwd": run_cwd}

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
            runtime.note("fallback", cwd=cwd, scope=scope, delegation=did, node=rec["node"],
                         cause=f"throwaway worktree failed: {out.strip()[:300]}",
                         route="throwaway-worktree -> run directory + porcelain check",
                         decision="readonly")
            worktree = None
        else:
            workdir = str(worktree)
    check_porcelain = bool(readonly) and (readonly not in READONLY_SANDBOXED or worktree is None)
    before = porcelain(run_cwd) if check_porcelain else None

    schema = executors.json_schema(returns)
    schema_file: Optional[str] = None
    if returns_route == "schema-flag":
        schema_file = str(st.write_receipt(did, json.dumps(schema, indent=1), ".schema.json"))
    json_output = bool(executors._cap(caps, "headless.output.json").enabled)

    errors: Optional[List[str]] = None
    try:
        for attempt in (1, 2):
            prompt = executors.build_prompt(brief, errors=errors, readonly=bool(readonly))
            argv, stdin = executors.build_argv(
                launch.command, caps, plan, prompt, harness=launch.harness, schema=schema,
                schema_file=schema_file, extra_args=launch.extra_args,
            )
            suffix = "" if attempt == 1 else f".{attempt}"
            out_path = st.receipt_path(did, f"{suffix}.out")
            err_path = st.receipt_path(did, f"{suffix}.err")
            runtime.delegate_update(did, cwd=cwd, scope=scope, attempt_running=attempt,
                                    stdout=str(out_path))
            code, timed_out = run_process(
                argv, env=launch.env, cwd=workdir, stdin=stdin, out_path=out_path,
                err_path=err_path, timeout=deadline - clock(), procs=procs,
            )
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
                runtime.delegate_submit(did, got[0], got[1], cwd=cwd, scope=scope)
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
        if check_porcelain:
            after = porcelain(run_cwd)
            if before is not None and after is not None and before != after:
                try:
                    runtime.note("monitor_violation", cwd=cwd, scope=scope, kind="readonly",
                                 delegation=did, node=rec["node"],
                                 detail="git status --porcelain changed during a read-only "
                                        "headless delegation")
                except Exception:  # noqa: BLE001 - the run may be gone
                    log.exception("could not journal a readonly violation")
                summary["readonly_violation"] = True
        if worktree is not None:
            code, out = git(run_cwd, "worktree", "remove", "--force", str(worktree))
            if code != 0:
                log.warning("could not remove throwaway worktree %s: %s", worktree, out.strip())


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


def spawn_body(brief: Dict[str, Any], *, cwd: str, scope: str, receipt_path: str,
               mcp: bool = True) -> Dict[str, Any]:
    return {
        "task": executors.session_task(brief, cwd=cwd, scope=scope,
                                       receipt_path=receipt_path, mcp=mcp),
        # No v1 run for the child (onboard.NO_WORKFLOW) and no board issue:
        # the delegation record is the child's whole assignment.
        "workflow": "-",
        "beads": False,
    }


def spawn_session(cwd: str, scope: str, did: str, *,
                  spawn: Callable[[str, Dict[str, Any]], str] = default_spawn) -> Dict[str, Any]:
    """Start a claimed ``session`` delegation: spawn the child and record it.
    A refusal moves the delegation to the node's next executor."""
    st = RunStore(cwd, scope)
    r = runtime.Run.load(st)
    rec = r.state["delegations"][did]
    brief = runtime.brief(r, did)
    mcp = ((rec.get("route") or {}).get("returns") or {}).get("route") != "receipt-file"
    st.receipts_dir.mkdir(parents=True, exist_ok=True)  # the child writes there
    body = spawn_body(brief, cwd=str(st.cwd), scope=st.scope,
                      receipt_path=str(st.receipt_path(did, ".child.json")), mcp=mcp)
    try:
        child = spawn(scope, body)
    except Exception as exc:  # noqa: BLE001 - every refusal reroutes
        nxt = runtime.delegate_reroute(did, f"session.spawn refused: {exc}", cwd=cwd, scope=scope)
        return {"delegation": did, "result": "rerouted", "executor": nxt, "error": str(exc)}
    runtime.delegate_update(did, cwd=cwd, scope=scope, child=child,
                            event={"event": "delegate_started", "phase": "spawned",
                                   "executor": ir.EXEC_SESSION, "child": child})
    return {"delegation": did, "result": "spawned", "child": child}
