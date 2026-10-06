"""The daemon clock of cflow v2 runs (``docs/cflow-v2.md`` sections 4, 6.4, 7, 8).

One periodic loop over every v2 run slot (``<cwd>/.cflow2/runs/<scope>``) in
the directories the daemon's live sessions stand in -- the scope is the
session name, as for v1 -- that does what no agent call does:

* **waits**: :func:`..cflow2.runtime.wait_tick` with the default probe runner
  (probes run outside the run lock; the runtime applies their results under
  it), so time/probe waits, timeouts and loop pauses fire without the driver
  calling ``flow_status``;
* **monitors**: :func:`..cflow2.runtime.monitors_tick` (``always`` probes on
  their ``every``, ``within`` deadlines over the journal); ``halt`` stops the
  run (``waiting_approval``/``monitor_violation``), ``notify`` and ``halt``
  are typed into the owning session the way the v1 timer clock delivers its
  notices (``session.queue_delivery``), ``record`` is the journal entry;
* **dispatch**: queued ``headless`` delegations run as harness processes
  (:func:`.cflow2_exec.run_headless`, worker threads, at most
  ``daemon.cflow2_headless_max`` at once) and queued ``session`` delegations
  become child sessions (:func:`.cflow2_exec.spawn_session`); a headless
  delegation left ``running`` by an earlier daemon is requeued;
* **budgets**: ``budget.seconds`` of ``subagent``/``session`` delegations
  (headless enforces its own by killing the process): the delegation ends
  ``timeout`` and the driver (and a child session) is told to stop;
* **asks**: :func:`..cflow2.asks.tick` -- a task's ``ask`` whose group
  timed out moves to the next ``from`` group, and every ask waiting for a
  group is routed (roster read, question delivered as a mesh ``decide``
  message); an ask that fell to a person or to the driver is typed into the
  owning session once;
* **children**: a ``session`` delegation whose child exited without
  submitting fails; one whose child wrote the receipt file (no MCP route) is
  submitted from it.

Every tick also writes the dispatch heartbeat
(:func:`..cflow2.executors.write_heartbeat`) listing the directories it
scans, which is how a runtime running in another process (the agent's MCP
server, the CLI) knows a queued delegation will be served instead of falling
back (:func:`..cflow2.executors.available`). In this process the clock also
registers a dispatcher (:func:`..cflow2.executors.register`) that leaves the
delegation queued for the next tick.

Switched by ``daemon.cflow2_clock`` (default on); without it nothing here
runs, no heartbeat is written, and v2 delegations fall back at once.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import os
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple

from .. import store
from ..cflow2 import asks, executors, ir, runtime
from ..cflow2 import store as cstore
from . import cflow2_exec

log = logging.getLogger("claunch.daemon.cflow2")

DEFAULT_POLL = 10.0
DEFAULT_HEADLESS_MAX = 3

Notice = Tuple[str, str, str]  # (cwd, session name, text)


def _resolve(cwd: str) -> str:
    try:
        return str(Path(cwd).resolve())
    except OSError:
        return str(cwd)


class Cflow2Clock:
    def __init__(
        self,
        manager: Any,
        *,
        poll: Optional[float] = None,
        spawn: Optional[Callable[[str, Dict[str, Any]], str]] = None,
        launch_for: Optional[Callable[[str], Optional[cflow2_exec.Launch]]] = None,
        probe_runner: Optional[Callable[..., Any]] = runtime.default_probe_runner,
        max_headless: Optional[int] = None,
        heartbeat: bool = True,
        threaded: bool = True,
        default_seconds: float = cflow2_exec.DEFAULT_SECONDS,
        ask_pool: Optional[Callable[..., Any]] = None,
        ask_deliver: Optional[Callable[..., Any]] = None,
    ) -> None:
        self.manager = manager
        self.poll = float(poll if poll is not None else DEFAULT_POLL)
        self.spawn = spawn or cflow2_exec.default_spawn
        self.launch_for = launch_for or self._launch_for
        self.probe_runner = probe_runner
        self.max_headless = max_headless
        self.heartbeat = heartbeat
        self.threaded = threaded
        self.default_seconds = default_seconds
        #: Roster reader and question delivery for asks (tests stub them;
        #: ``None`` is the mesh through this daemon's API -- :meth:`scan`
        #: runs in a worker thread, so the HTTP call does not block the loop).
        self.ask_pool = ask_pool
        self.ask_deliver = ask_deliver
        #: Identifies this daemon process on a claimed delegation; a running
        #: headless record carrying another boot was orphaned by a restart.
        self.boot = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.procs = cflow2_exec.Procs()
        self.jobs: Dict[Tuple[str, str, str], threading.Thread] = {}
        self.results: List[Dict[str, Any]] = []
        #: Directories a run was started in through this process's runtime
        #: (the in-process dispatcher saw it) without a session standing there.
        self.extra_cwds: Set[str] = set()
        self._lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        executors.register(self._dispatch_hook)
        self._task = self._loop.create_task(self._run())

    async def shutdown(self) -> None:
        executors.register(None)
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task
        if self.heartbeat:
            executors.clear_heartbeat()
        # A restart requeues these delegations; a process left running would
        # deliver its result next to the re-run's.
        self.procs.kill_all()

    def _dispatch_hook(self, rec: dict, run: Any) -> str:
        """The in-process dispatcher: the runtime ran inside the daemon (an API
        call). Leave it queued, make sure its directory is scanned, wake up."""
        with self._lock:
            self.extra_cwds.add(_resolve(str(run.store.cwd)))
        loop, wake = self._loop, self._wake
        if loop is not None and wake is not None:
            with contextlib.suppress(RuntimeError):
                loop.call_soon_threadsafe(wake.set)
        return "queued"

    async def _run(self) -> None:
        assert self._wake is not None
        while True:
            try:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll)
                self._wake.clear()
                for cwd, name, text in await asyncio.to_thread(self.scan):
                    self._deliver(cwd, name, text)
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cflow2 clock tick failed")

    def _deliver(self, cwd: str, name: str, text: str) -> None:
        try:
            session = self.manager.get(name)
        except Exception:
            return
        if getattr(session, "exited", True):
            return
        try:
            session.queue_delivery(text)
        except Exception:
            log.exception("cflow2 notice delivery to %r failed", name)

    # -- what to scan ----------------------------------------------------
    def _live_sessions(self) -> List[Any]:
        try:
            sessions = self.manager.list()
        except Exception:
            return []
        return [s for s in sessions if not getattr(s, "exited", True)]

    def _live(self, name: Optional[str]) -> Optional[Any]:
        if not name:
            return None
        try:
            session = self.manager.get(name)
        except Exception:
            return None
        return None if getattr(session, "exited", True) else session

    def cwds(self) -> List[str]:
        out: Set[str] = set()
        for session in self._live_sessions():
            cwd = getattr(getattr(session, "sdef", None), "cwd", "") or ""
            if cwd:
                out.add(_resolve(cwd))
        with self._lock:
            extra = set(self.extra_cwds)
        for cwd in extra:
            if cstore.list_scopes(cwd):
                out.add(cwd)
            else:
                with self._lock:
                    self.extra_cwds.discard(cwd)
        return sorted(out)

    def _launch_for(self, scope: str) -> Optional[cflow2_exec.Launch]:
        session = self._live(scope)
        if session is None:
            return None
        return cflow2_exec.launch_for_session(session)

    def _headless_max(self) -> int:
        if self.max_headless is not None:
            return int(self.max_headless)
        try:
            return max(1, int(store.daemon_config().get("cflow2_headless_max")
                              or DEFAULT_HEADLESS_MAX))
        except (TypeError, ValueError):
            return DEFAULT_HEADLESS_MAX

    def running_jobs(self) -> int:
        with self._lock:
            for key in [k for k, t in self.jobs.items() if not t.is_alive()]:
                del self.jobs[key]
            return len(self.jobs)

    def wait_jobs(self, timeout: float = 30.0) -> None:
        """Join the headless workers (tests)."""
        deadline = time.monotonic() + timeout
        while self.running_jobs():
            with self._lock:
                threads = list(self.jobs.values())
            for t in threads:
                t.join(max(0.0, deadline - time.monotonic()))
            if time.monotonic() >= deadline:
                break

    # -- the tick ----------------------------------------------------------
    def scan(self, now: Optional[float] = None) -> List[Notice]:
        """One pass over every run. Blocking (probes, file writes, a spawn
        call to this daemon's API): run it in a thread. Returns the notices
        to type into sessions."""
        cwds = self.cwds()
        if self.heartbeat:
            try:
                executors.write_heartbeat(cwds, self.poll)
            except OSError:
                log.exception("cannot write the cflow2 dispatch heartbeat")
        notices: List[Notice] = []
        for cwd in cwds:
            for scope in cstore.list_scopes(cwd):
                try:
                    self.tick_slot(cwd, scope, now, notices)
                except (runtime.FlowError, cstore.StoreError) as exc:
                    log.debug("cflow2 clock skipped %s/%s: %s", cwd, scope, exc)
                except Exception:
                    log.exception("cflow2 clock failed on %s/%s", cwd, scope)
        return notices

    def tick_slot(self, cwd: str, scope: str, now: Optional[float],
                  notices: List[Notice]) -> None:
        st = cstore.RunStore(cwd, scope)
        if st.load_state().get("status") not in runtime.LIVE_STATUSES:
            return
        runtime.wait_tick(now, self.probe_runner, cwd=cwd, scope=scope)
        for text in asks.tick(cwd=cwd, scope=scope, now=now, pool_fn=self.ask_pool,
                              deliver_fn=self.ask_deliver):
            notices.append((cwd, scope, text))
        for v in runtime.monitors_tick(now, self.probe_runner, cwd=cwd, scope=scope):
            if v["action"] in (runtime.MON_NOTIFY, runtime.MON_HALT):
                notices.append((cwd, scope, monitor_block(scope, v)))
        state = st.load_state()
        if state.get("status") not in runtime.LIVE_STATUSES:
            return
        clock_now = time.time() if now is None else float(now)
        for did, rec in (state.get("delegations") or {}).items():
            if rec.get("status") not in runtime.OPEN_DELEGATION:
                continue
            ex = rec.get("executor")
            if ex == ir.EXEC_HEADLESS:
                self._headless(cwd, scope, did, rec, clock_now)
            elif ex == ir.EXEC_SESSION:
                self._session(cwd, scope, did, rec, clock_now, notices)
            elif ex == ir.EXEC_SUBAGENT:
                self._budget(cwd, scope, did, rec, clock_now, notices)

    # -- executors ---------------------------------------------------------
    def _headless(self, cwd: str, scope: str, did: str, rec: dict, now: float) -> None:
        key = (cwd, scope, did)
        status = rec.get("status")
        if status == "running":
            runner = rec.get("runner") or {}
            with self._lock:
                mine = key in self.jobs
            if mine:
                return
            if runner.get("boot") != self.boot:
                runtime.delegate_update(
                    did, expect=("running",), cwd=cwd, scope=scope, status="queued",
                    runner=None,
                    event={"event": "fallback", "cause": "the daemon that ran this headless "
                           "delegation is gone", "route": "requeue", "decision": "executor"},
                )
                return
            # Our own worker finished without closing the record.
            with contextlib.suppress(runtime.FlowError):
                runtime.delegate_fail(did, "the headless executor ended without a result",
                                      cwd=cwd, scope=scope)
            return
        if status != "queued" or self.running_jobs() >= self._headless_max():
            return
        launch = self.launch_for(scope)
        if launch is None:
            runtime.delegate_reroute(
                did, f"no live session {scope!r} to take the profile environment from",
                cwd=cwd, scope=scope)
            return
        claimed = runtime.delegate_update(
            did, expect=("queued",), cwd=cwd, scope=scope, status="running",
            runner={"boot": self.boot, "harness": launch.harness}, dispatched_at=now,
            event={"event": "delegate_started", "phase": "dispatched",
                   "executor": ir.EXEC_HEADLESS, "harness": launch.harness},
        )
        if not claimed:
            return

        def job() -> None:
            try:
                result = cflow2_exec.run_headless(
                    cwd, scope, did, launch, default_seconds=self.default_seconds,
                    procs=self.procs)
            except Exception as exc:  # noqa: BLE001 - must not leave it running forever
                log.exception("headless delegation %s/%s/%s crashed", cwd, scope, did)
                with contextlib.suppress(Exception):
                    runtime.delegate_fail(did, f"headless executor error: {exc}",
                                          cwd=cwd, scope=scope)
                result = {"delegation": did, "result": "error", "error": str(exc)}
            log.info("cflow2 headless %s/%s %s: %s", scope, did, cwd, result.get("result"))
            with self._lock:
                self.results.append(result)

        if not self.threaded:
            job()
            return
        thread = threading.Thread(target=job, name=f"cflow2-headless-{scope}-{did}",
                                  daemon=True)
        with self._lock:
            self.jobs[key] = thread
        thread.start()

    def _session(self, cwd: str, scope: str, did: str, rec: dict, now: float,
                 notices: List[Notice]) -> None:
        status = rec.get("status")
        if status == "queued":
            if self._live(scope) is None:
                runtime.delegate_reroute(
                    did, f"no live session {scope!r} to spawn a child session from",
                    cwd=cwd, scope=scope)
                return
            claimed = runtime.delegate_update(
                did, expect=("queued",), cwd=cwd, scope=scope, status="running",
                runner={"boot": self.boot}, dispatched_at=now)
            if claimed:
                result = cflow2_exec.spawn_session(cwd, scope, did, spawn=self.spawn)
                with self._lock:
                    self.results.append(result)
            return
        if status != "running":
            return
        if self._budget(cwd, scope, did, rec, now, notices):
            return
        st = cstore.RunStore(cwd, scope)
        receipt = st.receipt_path(did, ".child.json")
        if receipt.is_file():
            taken = receipt.with_name(f"{did}.child.{int(time.time())}.json")
            with contextlib.suppress(OSError):
                receipt.rename(taken)
            try:
                runtime.delegate_submit(did, receipt_path=str(taken), cwd=cwd, scope=scope)
            except runtime.FlowError as exc:
                child = rec.get("child")
                if child:
                    notices.append((cwd, child, f"[cflow2] your result for delegation {did} "
                                    f"was refused: {exc}"))
            return
        child = rec.get("child")
        if child and self._live(child) is None:
            runtime.delegate_fail(
                did, f"child session {child!r} ended without submitting a result",
                cwd=cwd, scope=scope)

    def _budget(self, cwd: str, scope: str, did: str, rec: dict, now: float,
                notices: List[Notice]) -> bool:
        """End a subagent/session delegation past its ``budget.seconds``."""
        seconds = ((rec.get("route") or {}).get("budget") or {}).get("seconds")
        if not seconds:
            return False
        started = rec.get("dispatched_at") or rec.get("started_at")
        if started is None or now - float(started) <= float(seconds):
            return False
        ex = rec.get("executor")
        runtime.delegate_fail(
            did, f"budget.seconds {float(seconds):g} ran out ({ex} delegation)",
            outcome=ir.OUTCOME_TIMEOUT, detail={"seconds": seconds}, cwd=cwd, scope=scope)
        label = rec.get("label") or rec.get("node")
        notices.append((cwd, scope, (
            f"[cflow2] delegation {did} ({label}, {ex}) ran past its budget of "
            f"{float(seconds):g}s and has been ended with outcome 'timeout'. Stop the "
            f"{'sub-agent' if ex == ir.EXEC_SUBAGENT else 'child session'} working on it; "
            "a late flow_delegate for it will be refused. Call flow_status for where the "
            "run went next.")))
        child = rec.get("child")
        if child:
            notices.append((cwd, child, (
                f"[cflow2] delegation {did} ran past its budget of {float(seconds):g}s and "
                "was ended as 'timeout' in your parent's run. Stop working on it; do not "
                "submit a result.")))
        return True


def monitor_block(scope: str, v: Dict[str, Any]) -> str:
    """What the owning session hears about a monitor violation."""
    if v.get("kind") == "always":
        what = f"the invariant `{v.get('expr')}` did not hold"
        if v.get("probe"):
            what += f" (probe values: {v.get('probe')})"
    else:
        what = (f"after {v.get('when')} at {v.get('since')}, no {v.get('expect')} followed "
                f"within {v.get('within'):g}s")
    lines = [f"[cflow2] monitor '{v.get('monitor')}' of run '{scope}': {what}."]
    if v.get("action") == runtime.MON_HALT:
        lines.append("The run is halted (waiting_approval, reason monitor_violation) until a "
                     "person resumes it. Do not work around it; tell the user what you see.")
    else:
        lines.append("The run continues; check whether this needs action.")
    return "\n".join(lines)


def enabled(cfg: Optional[dict] = None) -> bool:
    cfg = store.daemon_config() if cfg is None else cfg
    return bool(cfg.get("cflow2_clock", True))


def poll_from(cfg: Optional[dict] = None) -> float:
    cfg = store.daemon_config() if cfg is None else cfg
    try:
        return max(1.0, float(cfg.get("cflow2_poll") or DEFAULT_POLL))
    except (TypeError, ValueError):
        return DEFAULT_POLL
