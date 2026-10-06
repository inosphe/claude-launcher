"""The daemon clock of cflow v2 runs (``docs/cflow-v2.md`` sections 4, 6.4, 7, 8).

One periodic loop over every v2 run slot (``<cwd>/.cflow2/runs/<scope>``) in
the directories the daemon's live sessions stand in -- the scope is the
session name, as for v1 -- that does what no agent call does:

* **waits**: :func:`..cflow2.runtime.wait_tick` with :func:`run_probe` (its
  own process group, run values only through the environment; probes run
  outside the run lock and the runtime applies their results under it), so time/probe waits, timeouts and loop pauses fire without the driver
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
  submitted from it;
* **triggers**: every slot's recorded ``triggers:`` are claimed once
  (:func:`..cflow2.runtime.claim_triggers`) during the pass and performed
  after it on the event loop, one at a time and each bounded
  (:func:`.cflow2_triggers.perform_bounded`), then journalled
  (:func:`..cflow2.runtime.complete_trigger`, which removes the record).
  Delivery is at least once: a claim is marked with this clock's boot, and
  one left by an earlier boot (a restart or a stop between claim and
  completion) or older than :data:`..cflow2.runtime.TRIGGER_CLAIM_STALE` is
  released (``trigger_requeued``) and performed again;
* **rounds**: a finished run of a ``recur: {auto: true}`` definition whose
  next round is requested is started again (:func:`..cflow2.runtime.start_next_round`)
  and the driver is told;
* **restarts**: an active task with ``restart:`` has its command claimed once
  per visit (:func:`..cflow2.runtime.claim_restarts`) and run after the pass
  in a worker thread with the driver's ``CLAUNCH_SESSION`` (v1's
  :meth:`.cflow_clock.RestartClock._execute`), its result recorded with
  :func:`..cflow2.runtime.complete_restart`;
* **wake-up**: a pass that moved a run on its own (a wait passed or timed
  out, a held choice's window opened) tells the driver when the run now
  waits on it (a task, or an agent's choice);
* **watch**: collection ``watch:`` probes that are due
  (:func:`..cflow2.runtime.watch_due`) run like a wait probe, with
  :data:`WATCH_TIMEOUT`; the exit code is applied by
  :func:`..cflow2.runtime.watch_apply` (a timed-out probe skips that round).

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
import hmac
import json
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
from . import cflow2_exec, cflow2_triggers

log = logging.getLogger("claunch.daemon.cflow2")

DEFAULT_POLL = 10.0
DEFAULT_HEADLESS_MAX = 3

Notice = Tuple[str, str, str]  # (cwd, session name, text)


def _resolve(cwd: str) -> str:
    try:
        return str(Path(cwd).resolve())
    except OSError:
        return str(cwd)


#: How often the heartbeat is rewritten while a pass is in progress (doc:
#: executors.HEARTBEAT_STALE is 90s; a pass blocked on a probe or a spawn
#: call must not let it go stale).
HEARTBEAT_EVERY = 20.0
#: A pass that has made no progress (no slot, probe or spawn started) for
#: this long is stuck: the heartbeat stops so delegations fall back.
PROGRESS_MAX = 600.0
#: How long a lost spawn's child is looked for after its delegation was
#: rerouted (the spawn call timed out at 120s; the daemon may finish later).
LOST_WATCH = 900.0
#: A child receipt that is not a complete ``{"outcome": ...}`` document is
#: left alone this long (it may still be being written) before it is
#: submitted anyway for validation to refuse.
RECEIPT_SETTLE = 30.0
#: Probe wall clock.
PROBE_TIMEOUT = 60.0
#: A collection watch probe's wall clock (docs 2.7, watch).
WATCH_TIMEOUT = 30.0
#: The exit code :func:`run_probe` gives a probe it killed on timeout.
PROBE_TIMED_OUT = 124
#: The output :func:`run_probe` gives with :data:`PROBE_TIMED_OUT`: a probe
#: that itself exits 124 is told apart from one that was killed.
PROBE_TIMED_OUT_TEXT = "probe timed out"


def run_probe(probe: Any, cwd: str, timeout: float = PROBE_TIMEOUT,
              env: Optional[Dict[str, str]] = None) -> Tuple[int, str]:
    """Run a wait or monitor probe: ``(exit code, output)``.

    The probe text is run as written; run values reach it only through
    ``env`` (the runtime's ``CFLOW_IN_*``/``CFLOW_DATA_*`` variables, added
    to the daemon's environment), never by being pasted into the command.
    The command runs in its own process group, killed whole on timeout
    (exit 124). Exit codes are returned as they are; what 0, 1 and others
    mean is the runtime's (doc 2.3, wait).
    """
    import signal
    import subprocess
    import sys

    cmd = probe if isinstance(probe, list) else str(probe)
    full_env = dict(os.environ)
    if env:
        full_env.update({str(k): str(v) for k, v in env.items()})
    kwargs: Dict[str, Any] = {}
    if sys.platform != "win32":
        kwargs["start_new_session"] = True
    try:
        proc = subprocess.Popen(cmd, shell=isinstance(cmd, str), cwd=cwd, env=full_env,
                                stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                stdin=subprocess.DEVNULL, text=True, **kwargs)
    except OSError as exc:
        return 127, str(exc)
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        with contextlib.suppress(OSError, ProcessLookupError):
            if sys.platform != "win32":
                os.killpg(proc.pid, signal.SIGKILL)
            else:  # pragma: no cover - windows
                proc.kill()
        with contextlib.suppress(Exception):
            proc.communicate(timeout=5)
        return PROBE_TIMED_OUT, PROBE_TIMED_OUT_TEXT
    return proc.returncode, out or ""


class Cflow2Clock:
    def __init__(
        self,
        manager: Any,
        *,
        poll: Optional[float] = None,
        spawn: Optional[Callable[[str, Dict[str, Any]], str]] = None,
        launch_for: Optional[Callable[[str], Optional[cflow2_exec.Launch]]] = None,
        probe_runner: Optional[Callable[..., Any]] = run_probe,
        max_headless: Optional[int] = None,
        heartbeat: bool = True,
        threaded: bool = True,
        default_seconds: float = cflow2_exec.DEFAULT_SECONDS,
        ask_pool: Optional[Callable[..., Any]] = None,
        ask_deliver: Optional[Callable[..., Any]] = None,
        find_child: Optional[cflow2_exec.FindChild] = None,
        kill_child: Optional[Callable[[str, str], Any]] = None,
        facts_fn: Optional[Callable[[str, str], Dict[str, Any]]] = None,
        restart_runner: Optional[Callable[[str, Dict[str, Any], str], Dict[str, Any]]] = None,
        platform: Optional[str] = None,
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
        #: ``(parent, marker) -> child name``: a child created for a session
        #: delegation, recognised by the marker line of its task.
        self.find_child = find_child or self._find_child
        #: ``(parent, child)``: end a child through the daemon's kill path.
        self.kill_child = kill_child or default_kill_child
        #: ``(cwd, session) -> $facts`` for trigger pushes (tests stub it;
        #: None measures git and the board, :func:`.cflow2_triggers.measure_facts`).
        self.facts_fn = facts_fn
        #: ``(cwd, action, session) -> {exit_code, output}`` runs a claimed
        #: restart command (tests stub it; None is v1's RestartClock._execute).
        self.restart_runner = restart_runner
        #: Host platform for a restart's command (None: platform.system()).
        self.platform = platform
        #: Restarts claimed during a pass, run after it: ``(cwd, scope, action)``.
        self.restarts: List[Tuple[str, str, Dict[str, Any]]] = []
        #: Identifies this daemon process on a claimed delegation; a running
        #: headless record carrying another boot was orphaned by a restart.
        self.boot = f"{os.getpid()}-{uuid.uuid4().hex[:8]}"
        self.procs = cflow2_exec.Procs()
        #: Set by :meth:`shutdown`: no new claim, spawn or attempt starts.
        self.stop = threading.Event()
        self.jobs: Dict[Tuple[str, str, str, str], threading.Thread] = {}
        self.results: List[Dict[str, Any]] = []
        #: Directories a run was started in through this process's runtime
        #: (the in-process dispatcher saw it) without a session standing there.
        self.extra_cwds: Set[str] = set()
        self._lock = threading.Lock()
        self._task: Optional[asyncio.Task] = None
        self._beat_task: Optional[asyncio.Task] = None
        self._wake: Optional[asyncio.Event] = None
        self._loop: Optional[asyncio.AbstractEventLoop] = None
        self._cwds: List[str] = []
        self._scanning = False
        self._progress = 0.0
        self._last_beat = 0.0
        #: Triggers claimed during a pass, performed after it:
        #: ``(cwd, scope, trigger record, driver)``; driver None is looked
        #: up when the trigger is performed.
        self.claimed: List[Tuple[str, str, Dict[str, Any], Optional[str]]] = []
        #: ``(cwd, scope, trigger id)`` claimed and not completed yet, so a
        #: stale re-claim of one still waiting here is not performed twice.
        self._pending_triggers: Set[Tuple[str, str, str]] = set()

    # -- lifecycle -------------------------------------------------------
    def start(self) -> None:
        if self._task is not None:
            return
        self._loop = asyncio.get_running_loop()
        self._wake = asyncio.Event()
        executors.register(self._dispatch_hook)
        self._task = self._loop.create_task(self._run())
        if self.heartbeat:
            self._beat_task = self._loop.create_task(self._beat_loop())

    async def shutdown(self) -> None:
        # Stop first: a worker whose process is killed below must requeue
        # its delegation, not start the second attempt (A5).
        self.stop.set()
        executors.register(None)
        self.procs.kill_all()
        for attr in ("_task", "_beat_task"):
            task = getattr(self, attr)
            setattr(self, attr, None)
            if task is not None:
                task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await task
        if self.heartbeat:
            executors.clear_heartbeat()
        # Let the killed workers write their requeue; whatever does not land
        # is requeued by the next boot (the record's pid is gone).
        with contextlib.suppress(Exception):
            await asyncio.to_thread(self.wait_jobs, 5.0)

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
        while not self.stop.is_set():
            try:
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(self._wake.wait(), timeout=self.poll)
                self._wake.clear()
                for cwd, name, text in await asyncio.to_thread(self.scan):
                    self._deliver(cwd, name, text)
                self._start_restarts()
                await self.run_triggers()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cflow2 clock tick failed")

    async def _beat_loop(self) -> None:
        """Keep the heartbeat fresh while a long pass blocks on a probe or a
        spawn call (D): written every :data:`HEARTBEAT_EVERY` as long as the
        pass keeps making progress."""
        while not self.stop.is_set():
            await asyncio.sleep(HEARTBEAT_EVERY)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(self._beat)

    def _mark(self) -> None:
        """A pass made progress (a slot, a probe, a spawn started)."""
        self._progress = time.time()

    def _beat(self, force: bool = False) -> None:
        if not self.heartbeat or self.stop.is_set():
            return
        now = time.time()
        if not force:
            if now - self._last_beat < HEARTBEAT_EVERY:
                return
            if self._scanning and now - self._progress > PROGRESS_MAX:
                return  # stuck: let it go stale so runs fall back
        try:
            executors.write_heartbeat(self._cwds, self.poll)
            self._last_beat = now
        except OSError:
            log.exception("cannot write the cflow2 dispatch heartbeat")

    def _probe(self, probe: Any, cwd: str, *args: Any, **kwargs: Any) -> Any:
        """The probe runner the runtime gets: marks progress and keeps the
        heartbeat fresh between probes."""
        self._mark()
        self._beat()
        assert self.probe_runner is not None
        return self.probe_runner(probe, cwd, *args, **kwargs)

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

    def _find_child(self, parent: str, marker: str) -> Optional[str]:
        """A child of ``parent`` whose task carries ``marker`` (live first)."""
        try:
            names = list(self.manager.children(parent))
        except Exception:
            return None
        found: List[Tuple[bool, str]] = []
        for name in names:
            try:
                session = self.manager.get(name)
            except Exception:
                continue
            task = str(getattr(getattr(session, "sdef", None), "task", "") or "")
            if marker in task:
                found.append((bool(getattr(session, "exited", False)), name))
        return sorted(found)[0][1] if found else None

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
        """Join the headless workers (tests, shutdown)."""
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
        if self.stop.is_set():
            return []
        cwds = self.cwds()
        self._cwds = cwds
        self._scanning = True
        self._mark()
        self._beat(force=True)
        notices: List[Notice] = []
        try:
            for cwd in cwds:
                for scope in cstore.list_scopes(cwd):
                    if self.stop.is_set():
                        return notices
                    self._mark()
                    self._beat()
                    try:
                        self.tick_slot(cwd, scope, now, notices)
                    except (runtime.FlowError, cstore.StoreError) as exc:
                        log.debug("cflow2 clock skipped %s/%s: %s", cwd, scope, exc)
                    except Exception:
                        log.exception("cflow2 clock failed on %s/%s", cwd, scope)
            self.reap()
        finally:
            self._scanning = False
        return notices

    def reap(self) -> List[Tuple[str, str, str, str]]:
        """End the headless processes whose work is gone (B5): the run is no
        longer live or is another run now, the delegation is no longer open
        (cancelled by a par settle, a timeout, an abort), or a halt monitor
        stopped the run. Returns the job keys killed."""
        killed = []
        for key in self.procs.keys():
            if not (isinstance(key, tuple) and len(key) == 4):
                continue
            cwd, scope, run_id, did = key
            try:
                state = cstore.RunStore(cwd, scope).load_state()
            except Exception:  # noqa: BLE001 - the slot is gone
                state = {}
            rec = (state.get("delegations") or {}).get(did) or {}
            why = None
            if state.get("run_id") != run_id or state.get("status") not in runtime.LIVE_STATUSES:
                why = cflow2_exec.KILL_CANCELLED
            elif rec.get("status") not in runtime.OPEN_DELEGATION:
                why = cflow2_exec.KILL_CANCELLED
            elif _halted(state):
                why = cflow2_exec.KILL_HALT
            if why and self.procs.kill(key, why):
                killed.append(key)
                with contextlib.suppress(Exception):
                    runtime.note("delegate_killed", cwd=cwd, scope=scope, delegation=did,
                                 node=rec.get("node"), why=why,
                                 detail="the run finished, was aborted or halted, or the "
                                        "delegation was cancelled: its headless process "
                                        "group was killed")
        return killed

    def tick_slot(self, cwd: str, scope: str, now: Optional[float],
                  notices: List[Notice]) -> None:
        st = cstore.RunStore(cwd, scope)
        first = st.load_state()
        if first.get("triggers"):
            # Before the liveness check: a trigger recorded as the run
            # finished (a leave of the last node) is still performed.
            self._claim(cwd, scope, first)
        if first.get("status") not in runtime.LIVE_STATUSES:
            if (first.get("next_round") or {}).get("auto") and not self.stop.is_set():
                started = runtime.start_next_round(cwd, scope, now)
                if started:
                    notices.append((cwd, started.get("driver") or scope, round_block(started)))
            return
        probe = self._probe if self.probe_runner is not None else None
        payload = runtime.wait_tick(now, probe, cwd=cwd, scope=scope)
        if payload.get("ticked") and _waits_on_driver(payload):
            notices.append((cwd, payload.get("driver") or scope, moved_block(scope, payload)))
        self._claim_restarts(cwd, scope, now, notices)
        for text in asks.tick(cwd=cwd, scope=scope, now=now, pool_fn=self.ask_pool,
                              deliver_fn=self.ask_deliver):
            notices.append((cwd, scope, text))
        for v in runtime.monitors_tick(now, probe, cwd=cwd, scope=scope):
            if v["action"] in (runtime.MON_NOTIFY, runtime.MON_HALT):
                notices.append((cwd, scope, monitor_block(scope, v)))
        self._watch_collections(cwd, scope, now)
        self._watch_lost(cwd, scope)
        state = st.load_state()
        if state.get("status") not in runtime.LIVE_STATUSES:
            return
        halted = _halted(state)
        clock_now = time.time() if now is None else float(now)
        run_id = str(state.get("run_id") or "")
        for did, rec in (state.get("delegations") or {}).items():
            if self.stop.is_set():
                return
            if rec.get("status") not in runtime.OPEN_DELEGATION:
                continue
            ex = rec.get("executor")
            if ex == ir.EXEC_HEADLESS:
                self._headless(cwd, scope, run_id, did, rec, clock_now, halted)
            elif ex == ir.EXEC_SESSION:
                self._session(cwd, scope, did, rec, clock_now, notices, halted)
            elif ex == ir.EXEC_SUBAGENT:
                self._budget(cwd, scope, did, rec, clock_now, notices)

    # -- restarts (docs 2.2, restart) ----------------------------------------
    def _claim_restarts(self, cwd: str, scope: str, now: Optional[float],
                        notices: List[Notice]) -> None:
        if self.stop.is_set():
            return
        import platform as platform_mod

        system = self.platform or platform_mod.system()
        try:
            actions = runtime.claim_restarts(platform=system, boot=self.boot, cwd=cwd,
                                             scope=scope, now=now)
        except (runtime.FlowError, cstore.StoreError) as exc:
            log.debug("cflow2 restart claim skipped %s/%s: %s", cwd, scope, exc)
            return
        for action in actions:
            notices.append((cwd, action.get("driver") or scope, restart_block(action)))
            if action.get("kind") == "run":
                with self._lock:
                    self.restarts.append((cwd, scope, action))

    def _start_restarts(self) -> None:
        """Run the restarts the last pass claimed, each in its own task (a
        restart may take up to its timeout; the clock keeps ticking)."""
        with self._lock:
            todo, self.restarts = self.restarts, []
        loop = self._loop
        for cwd, scope, action in todo:
            if loop is None:
                continue
            loop.create_task(self._restart_task(cwd, scope, action))

    async def _restart_task(self, cwd: str, scope: str, action: Dict[str, Any]) -> None:
        try:
            for c, name, text in await asyncio.to_thread(self.perform_restart, cwd, scope,
                                                         action):
                self._deliver(c, name, text)
        except Exception:  # noqa: BLE001 - one restart must not stop the clock
            log.exception("cflow2 restart %s/%s failed", cwd, scope)

    def perform_restart(self, cwd: str, scope: str, action: Dict[str, Any]) -> List[Notice]:
        """Run one claimed restart and record it. Blocking. The command runs
        with the driver's ``CLAUNCH_SESSION`` so a ``claunch daemon restart``
        inside it goes through the web UI's approval gate."""
        session = str(action.get("driver") or scope)
        runner = self.restart_runner
        if runner is None:
            from .cflow_clock import RestartClock

            def runner(c: str, a: Dict[str, Any], sess: str) -> Dict[str, Any]:
                return RestartClock._execute(c, a, sess)
        result = runner(cwd, action, session)
        done = runtime.complete_restart(str(action.get("instance")), str(action.get("token")),
                                        result.get("exit_code"), str(result.get("output") or ""),
                                        cwd=cwd, scope=scope)
        if not done:
            return []
        return [(cwd, done.get("driver") or scope, restart_result_block(done))]

    # -- triggers and collection watch ---------------------------------------
    def _claim(self, cwd: str, scope: str, state: Dict[str, Any]) -> None:
        """Claim the slot's unclaimed (or released) triggers for
        :meth:`run_triggers`; the records stay in the run until completed."""
        if self.stop.is_set():
            return
        try:
            with self._lock:
                held = [t for c, sc, t in self._pending_triggers if c == cwd and sc == scope]
            claimed = runtime.claim_triggers(cwd, scope, boot=self.boot, held=held)
        except (runtime.FlowError, cstore.StoreError) as exc:
            log.debug("cflow2 trigger claim skipped %s/%s: %s", cwd, scope, exc)
            return
        with self._lock:
            for trig in claimed or []:
                key = (cwd, scope, str(trig.get("id") or ""))
                if key in self._pending_triggers:
                    continue
                self._pending_triggers.add(key)
                # The acting session is read when the trigger is performed
                # (runtime.acting_session): a session delegation's child may
                # be spawned after its nested run opened.
                self.claimed.append((cwd, scope, dict(trig), None))

    async def run_triggers(self) -> List[Tuple[str, str, str, bool, str]]:
        """Perform what the last pass claimed, in order, one at a time (a
        slow briefing endpoint delays the next pass; the heartbeat has its
        own task). Returns ``(cwd, scope, trigger id, performed, detail)``."""
        with self._lock:
            todo, self.claimed = self.claimed, []
        done: List[Tuple[str, str, str, bool, str]] = []
        cwds = list(self._cwds) or await asyncio.to_thread(self.cwds)
        for cwd, scope, trig, driver in todo:
            if self.stop.is_set():
                # Not performed: the record stays claimed by this boot and
                # the next boot performs it again (docs 2.8, at least once).
                break
            tid = str(trig.get("id") or "")
            self._mark()
            performed, detail = await cflow2_triggers.perform_bounded(
                self.manager, cwd, scope, trig, cwds=cwds, driver=driver,
                facts_fn=self.facts_fn)
            try:
                await asyncio.to_thread(runtime.complete_trigger, cwd, scope, tid,
                                        performed, detail)
            except Exception:  # noqa: BLE001 - the slot may be gone
                log.exception("cflow2 trigger completion %s/%s/%s failed", cwd, scope, tid)
            with self._lock:
                self._pending_triggers.discard((cwd, scope, tid))
            done.append((cwd, scope, tid, performed, detail))
        return done

    def _watch_collections(self, cwd: str, scope: str, now: Optional[float]) -> None:
        """Run the due collection watch probes and apply their exit codes."""
        if self.probe_runner is None:
            return
        clock_now = time.time() if now is None else float(now)
        try:
            due = runtime.watch_due(cwd, scope, clock_now)
        except (runtime.FlowError, cstore.StoreError) as exc:
            log.debug("cflow2 watch skipped %s/%s: %s", cwd, scope, exc)
            return
        for entry in due or []:
            if self.stop.is_set():
                return
            name, key, probe, env, timeout, where = _due_fields(entry)
            code, out = self._probe(probe, where or cwd, timeout, env=env)
            if code == PROBE_TIMED_OUT and out == PROBE_TIMED_OUT_TEXT:
                continue  # over the time limit: skip this round (docs 2.7)
            try:
                runtime.watch_apply(cwd, scope, name, key, code, clock_now)
            except (runtime.FlowError, cstore.StoreError) as exc:
                log.debug("cflow2 watch apply %s/%s %s[%s]: %s", cwd, scope, name, key, exc)

    # -- executors ---------------------------------------------------------
    def _requeue_orphan(self, cwd: str, scope: str, did: str, cause: str) -> bool:
        return runtime.delegate_update(
            did, expect=("running",), cwd=cwd, scope=scope, status="queued",
            runner=None, child=None, attempt_running=None,
            event={"event": "fallback", "cause": cause, "route": "requeue",
                   "decision": "executor"},
        )

    def _headless(self, cwd: str, scope: str, run_id: str, did: str, rec: dict,
                  now: float, halted: bool = False) -> None:
        key = (cwd, scope, run_id, did)
        status = rec.get("status")
        if status == "running":
            runner = rec.get("runner") or {}
            with self._lock:
                mine = key in self.jobs
            if mine:
                return
            if runner.get("boot") != self.boot:
                # Claimed by a daemon that is gone (A5). Its process may
                # have outlived it: end it first, never run the work twice.
                alive = cflow2_exec.process_alive(runner.get("pid"), runner.get("argv0"))
                if alive is None:
                    # A live pid we cannot identify: wait for it to go, or
                    # for the budget to run out, before running it again.
                    started = float(runner.get("started_at") or rec.get("dispatched_at") or now)
                    seconds = float(((rec.get("route") or {}).get("budget") or {}).get("seconds")
                                    or self.default_seconds)
                    if now - started <= seconds:
                        return
                elif alive:
                    cflow2_exec.kill_group(int(runner["pid"]))
                self._requeue_orphan(
                    cwd, scope, did,
                    "the daemon that ran this headless delegation is gone"
                    + (" (its process was killed)" if alive else ""))
                return
            # Our own worker finished without closing the record.
            with contextlib.suppress(runtime.FlowError):
                runtime.delegate_fail(did, "the headless executor ended without a result",
                                      cwd=cwd, scope=scope)
            return
        if status != "queued" or halted or self.stop.is_set():
            return
        if self.running_jobs() >= self._headless_max():
            return
        launch = self.launch_for(scope)
        if launch is None:
            runtime.delegate_reroute(
                did, f"no live session {scope!r} to take the profile environment from",
                cwd=cwd, scope=scope)
            return
        runner = {"boot": self.boot, "harness": launch.harness}
        claimed = runtime.delegate_update(
            did, expect=("queued",), cwd=cwd, scope=scope, status="running",
            runner=runner, dispatched_at=now,
            event={"event": "delegate_started", "phase": "dispatched",
                   "executor": ir.EXEC_HEADLESS, "harness": launch.harness},
        )
        if not claimed:
            return

        def job() -> None:
            try:
                result = cflow2_exec.run_headless(
                    cwd, scope, did, launch, default_seconds=self.default_seconds,
                    procs=self.procs, key=key, runner=runner)
            except Exception as exc:  # noqa: BLE001 - must not leave it running forever
                log.exception("headless delegation %s/%s/%s crashed", cwd, scope, did)
                if self.stop.is_set():
                    result = {"delegation": did, "result": "interrupted", "error": str(exc)}
                else:
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
                 notices: List[Notice], halted: bool = False) -> None:
        status = rec.get("status")
        st = cstore.RunStore(cwd, scope)
        if status == "queued":
            if halted or self.stop.is_set():
                return
            if self._live(scope) is None:
                runtime.delegate_reroute(
                    did, f"no live session {scope!r} to spawn a child session from",
                    cwd=cwd, scope=scope)
                return
            run_id = st.load_state().get("run_id")
            runner = {"boot": self.boot, "phase": cflow2_exec.PHASE_SPAWNING,
                      "marker": cflow2_exec.delegation_marker(st, run_id, did)}
            # Persisted before the spawn call (B6): a daemon that dies in
            # between leaves a record the next boot recognises.
            claimed = runtime.delegate_update(
                did, expect=("queued",), cwd=cwd, scope=scope, status="running",
                runner=runner, dispatched_at=now)
            if claimed:
                self._mark()
                self._beat()
                result = cflow2_exec.spawn_session(
                    cwd, scope, did, spawn=self.spawn, find_child=self.find_child,
                    runner=runner, lost=lambda marker: self._remember_lost(st, did, marker))
                with self._lock:
                    self.results.append(result)
            return
        if status != "running":
            return
        runner = rec.get("runner") or {}
        if not rec.get("child"):
            # Claimed, child never recorded: the spawn was interrupted (a
            # restart between the claim and the answer) or crashed.
            marker = runner.get("marker")
            found = None
            if marker:
                with contextlib.suppress(Exception):
                    found = self.find_child(scope, marker)
            if found:
                cflow2_exec._record_child(cwd, scope, did, found,
                                          dict(runner, boot=self.boot), marker,
                                          adopted="spawn interrupted")
                return
            if int(rec.get("respawns") or 0) >= 1:
                runtime.delegate_reroute(
                    did, "session spawn was interrupted twice without a child",
                    cwd=cwd, scope=scope)
                return
            runtime.delegate_update(
                did, expect=("running",), cwd=cwd, scope=scope, status="queued",
                runner=None, respawns=int(rec.get("respawns") or 0) + 1,
                event={"event": "fallback", "cause": "session spawn interrupted before a "
                       "child was recorded", "route": "requeue", "decision": "executor"})
            return
        if self._budget(cwd, scope, did, rec, now, notices):
            return
        if self._child_receipt(cwd, scope, did, rec, notices):
            return
        child = rec.get("child")
        if child and self._live(child) is None:
            runtime.delegate_fail(
                did, f"child session {child!r} ended without submitting a result",
                cwd=cwd, scope=scope)

    def _child_receipt(self, cwd: str, scope: str, did: str, rec: dict,
                       notices: List[Notice]) -> bool:
        """Submit a child's receipt file (no MCP route), only once it is
        complete (D): a whole JSON document with an ``outcome``, or one that
        has not changed for :data:`RECEIPT_SETTLE` seconds. It is renamed
        aside first; a failed rename submits nothing (next tick retries)."""
        st = cstore.RunStore(cwd, scope)
        receipt = st.receipt_path(did, ".child.json")
        try:
            text = receipt.read_text(encoding="utf-8")
            age = time.time() - receipt.stat().st_mtime
        except OSError:
            return False
        try:
            doc = json.loads(text)
            complete = isinstance(doc, dict) and "outcome" in doc
        except ValueError:
            complete = False
        if not complete and age < RECEIPT_SETTLE:
            return False
        # G1: the file is the child's only when it carries the key from the
        # child's task (hash kept on the record). A nested delegation, or a
        # child that submits over MCP, has no key: any file is refused.
        want = (rec.get("runner") or {}).get("receipt_key")
        got = doc.get("key") if complete else None
        if not want or not isinstance(got, str) \
                or not hmac.compare_digest(cflow2_exec.receipt_key_hash(got), want):
            refused = receipt.with_name(f"{did}.refused.{int(time.time() * 1000)}.json")
            with contextlib.suppress(OSError):
                receipt.rename(refused)
            with contextlib.suppress(Exception):
                runtime.note("receipt_refused", cwd=cwd, scope=scope, delegation=did,
                             path=str(refused),
                             detail="a child receipt without this delegation's key was "
                                    "set aside unread" if want else
                                    "this delegation takes no receipt file")
            child = rec.get("child")
            if child:
                notices.append((cwd, child, (
                    f"[cflow2] the receipt file for delegation {did} was refused: "
                    + ("it must be complete JSON with the \"key\" from your task"
                       if want else "submit with the flow_delegate MCP tool instead"))))
            return True
        taken = receipt.with_name(f"{did}.child.{int(time.time() * 1000)}.json")
        try:
            receipt.rename(taken)
        except OSError as exc:
            log.warning("cannot take child receipt %s: %s", receipt, exc)
            return False
        try:
            runtime.delegate_submit(did, receipt_path=str(taken), cwd=cwd, scope=scope)
        except runtime.FlowError as exc:
            child = rec.get("child")
            if child:
                notices.append((cwd, child, f"[cflow2] your result for delegation {did} "
                                f"was refused: {exc}"))
        return True

    # -- lost spawns -------------------------------------------------------
    def _remember_lost(self, st: cstore.RunStore, did: str, marker: str) -> None:
        """A spawn call failed after it may have reached the daemon: watch
        for a child carrying ``marker`` and end it (B6)."""
        with contextlib.suppress(OSError):
            st.write_receipt(did, json.dumps({"marker": marker, "since": time.time(),
                                              "parent": st.scope}), ".lost.json")

    def _watch_lost(self, cwd: str, scope: str) -> None:
        st = cstore.RunStore(cwd, scope)
        try:
            files = sorted(st.receipts_dir.glob("*.lost.json"))
        except OSError:
            return
        if not files:
            return
        dels = st.load_state().get("delegations") or {}
        for path in files:
            did = path.name[: -len(".lost.json")]
            try:
                doc = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                doc = {}
            marker = doc.get("marker")
            child = None
            if marker:
                with contextlib.suppress(Exception):
                    child = self.find_child(scope, marker)
            rec = dels.get(did) or {}
            if child and child == rec.get("child"):
                with contextlib.suppress(OSError):
                    path.unlink()  # it was adopted after all
                continue
            if child:
                outcome = "killed"
                try:
                    self.kill_child(scope, child)
                except Exception as exc:  # noqa: BLE001 - journal what we could not do
                    outcome = f"kill failed: {exc}"
                with contextlib.suppress(Exception):
                    runtime.note("orphan_child", cwd=cwd, scope=scope, delegation=did,
                                 child=child, outcome=outcome,
                                 detail="a child spawned for this delegation appeared after "
                                        "the spawn call failed and the delegation was "
                                        "rerouted; it was ended through the daemon's kill path")
                with contextlib.suppress(OSError):
                    path.unlink()
                continue
            if time.time() - float(doc.get("since") or 0) > LOST_WATCH:
                with contextlib.suppress(OSError):
                    path.unlink()

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


def _due_fields(entry: Any) -> Tuple[str, Any, Any, Dict[str, str], float, Optional[str]]:
    """``(collection, key, probe, env, timeout, cwd)`` of one
    :func:`runtime.watch_due` row (``{collection, key, probe, env, on,
    timeout, cwd}``, or a ``(collection, key, probe, env, on)`` tuple)."""
    if isinstance(entry, dict):
        try:
            timeout = float(entry.get("timeout") or WATCH_TIMEOUT)
        except (TypeError, ValueError):
            timeout = WATCH_TIMEOUT
        return (str(entry.get("collection")), entry.get("key"), entry.get("probe"),
                dict(entry.get("env") or {}), timeout, entry.get("cwd") or None)
    name, key, probe, env = entry[0], entry[1], entry[2], entry[3]
    return str(name), key, probe, dict(env or {}), WATCH_TIMEOUT, None


def _halted(state: Dict[str, Any]) -> bool:
    """A ``halt`` monitor stopped the run (until a person releases it)."""
    return any((a or {}).get("kind") == "monitor_violation"
               for a in state.get("approvals") or [])


def default_kill_child(parent: str, child: str) -> Any:
    """End ``child`` through the daemon's own route for a parent ending a
    session it spawned (``POST /api/sessions/<parent>/children/<child>/kill``):
    the same policy check, wind-down and exit handling as any kill. Blocking
    (an HTTP call to this daemon): the clock's pass runs in a worker thread."""
    from .. import daemon_client

    client = daemon_client.connect()
    if client is None:
        raise RuntimeError("the daemon API is not reachable")
    return client.post(f"/api/sessions/{parent}/children/{child}/kill", {}, timeout=60.0)


def _waits_on_driver(payload: Dict[str, Any]) -> bool:
    """The run now needs its driver: an active task or an agent's choice."""
    if payload.get("tasks"):
        return True
    return any(c.get("chooser") == ir.CHOOSER_AGENT and not c.get("held")
               for c in payload.get("choices") or [])


def moved_block(scope: str, payload: Dict[str, Any]) -> str:
    """The wake-up after the clock moved a run (a wait, a window)."""
    at = [t.get("label") or t.get("instance") for t in payload.get("tasks") or []]
    at += [c.get("label") or c.get("instance") for c in payload.get("choices") or []
           if c.get("chooser") == ir.CHOOSER_AGENT and not c.get("held")]
    return "\n".join([
        "---",
        "# claunch cflow2: the run moved -- machine-generated, not typed by the user",
        f"run: {scope} ({payload.get('workflow')}, {payload.get('run')})",
        f"waiting on you: {', '.join(str(a) for a in at)}",
        "protocol: the daemon's clock moved the run on its own (a wait passed or timed out, "
        "or a held choice's window opened). Call flow_status and continue.",
        "---"])


def round_block(started: Dict[str, Any]) -> str:
    """The wake-up after the clock started a recurring run's next round."""
    return "\n".join([
        "---",
        "# claunch cflow2: next round started -- machine-generated, not typed by the user",
        f"workflow: {started.get('workflow')}",
        f"round: {started.get('round')} (run {started.get('run')}, after {started.get('previous')})",
        "protocol: the definition declares recur {auto: true}, so the daemon started this "
        "round. Call flow_status and continue. A person stops the loop with "
        "'claunch cflow2 recur --cancel' once a round has finished.",
        "---"])


def restart_block(action: Dict[str, Any]) -> str:
    kind = action.get("kind")
    if kind == "run":
        event = ("the daemon is running this task's restart command from the run's directory "
                 "on the workflow's behalf -- do not run it yourself: an agent session has no "
                 "authority to restart or stop the daemon")
    elif kind == "interrupted":
        event = ("the daemon boot that ran this restart stopped while it was running; it is "
                 "recorded as interrupted and will not run twice in this visit")
    else:
        event = f"no restart command is declared for platform {action.get('platform')!r}"
    return "\n".join([
        "---",
        "# claunch cflow2: restart -- machine-generated, not typed by the user",
        f"workflow: {action.get('workflow')}",
        f"task: {action.get('label')} ({action.get('instance')})",
        f"event: {event}",
        "protocol: read flow_status after this message; the task's report is accepted once "
        "the restart result is in, and the run's wait decides whether deployment advances.",
        "---"])


def restart_result_block(done: Dict[str, Any]) -> str:
    return "\n".join([
        "---",
        "# claunch cflow2: restart result -- machine-generated, not typed by the user",
        f"workflow: {done.get('workflow')}",
        f"task: {done.get('label')} ({done.get('instance')})",
        f"exit code: {done.get('exit_code')} ({done.get('status')})",
        "protocol: the result is journalled (restart_completed); report the task with the "
        "evidence its done_when asks for.",
        "---"])


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
