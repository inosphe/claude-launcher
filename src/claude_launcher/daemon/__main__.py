"""Daemon process entrypoint: ``python -m claude_launcher.daemon``.

Started detached by the CLI's auto-start (or ``claunch daemon start``); runs
until ``POST /api/daemon/shutdown`` (or SIGINT when run in the foreground with
``--foreground`` for debugging).
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import os
import sys
import time
from typing import Optional

from aiohttp import web

from .. import daemon_client, providers, session_env, store
from . import basetemp_reaper
from . import cflow2_clock as cflow2_clock_mod
from . import cflow_clock, paths, restart_notice, resume, runtime_state, window as window_mod
from . import harness as harness_mod
from . import hold as hold_mod, instance_manifest, operator_keepalive, restore_queue, session_procs, session_reminder
from .api import build_app, close_websockets, notify_shutdown
from .manager import SessionManager
from .mesh import MeshError, MeshManager

log = logging.getLogger("claunch.daemon")

#: What ``_serve`` returns when the shutdown it drained was a restart request
#: (``POST /api/daemon/restart``): ``main`` spawns the successor only after
#: releasing the singleton lock, so the new daemon finds it free instead of
#: spending its grace window waiting this process out. Never a process exit
#: code — the restarting daemon itself still exits 0.
RESTART_CODE = 75

#: The port a restarting named instance hands its successor (see
#: :func:`_successor_env`). A variable of its own that the successor removes
#: as it starts: handed over as ``CLAUNCH_DAEMON_PORT`` it stayed in the
#: successor's environment, reached every session it launched, and a session's
#: ``claunch -L other`` then auto-started the other instance on this one's
#: port (claunch-d55af).
SUCCESSOR_PORT_ENV = "CLAUNCH_SUCCESSOR_PORT"


def _setup_logging(foreground: bool) -> None:
    handlers = [logging.StreamHandler(sys.stderr)]
    try:
        paths.daemon_dir().mkdir(parents=True, exist_ok=True)
        handlers.append(logging.FileHandler(paths.log_file(), encoding="utf-8"))
    except OSError:
        pass
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        handlers=handlers if foreground else handlers[1:] or handlers,
    )


def _quiet_reset_errors(loop: asyncio.AbstractEventLoop) -> None:
    """Stop a peer's abrupt disconnect from logging as a daemon error.

    On Windows the proactor closes a socket by calling ``shutdown()`` on it
    from a callback; when the far end already sent a reset (WinError 10054 —
    a closed browser tab, a killed ``claunch attach``) that call raises inside
    asyncio itself, with no coroutine to receive it, and the default handler
    reports it at ERROR. Nothing is wrong and nothing can be done about it
    from here — the connection is over either way — so a connection reset with
    no task behind it is demoted to debug and everything else keeps the
    default handling.
    """
    default = loop.get_exception_handler()

    def handler(loop_: asyncio.AbstractEventLoop, context: dict) -> None:
        exc = context.get("exception")
        if isinstance(exc, ConnectionResetError) and context.get("future") is None:
            log.debug("connection reset by peer: %s", context.get("message"))
            return
        if default is None:
            loop_.default_exception_handler(context)
        else:
            default(loop_, context)

    loop.set_exception_handler(handler)


#: Seconds a starting daemon waits for a PTY host still held by its
#: predecessor's connection (see :func:`_connect_pty_host`).
HOST_BUSY_WAIT = 15.0


def _connect_pty_host(cfg: dict):
    """The PTY host client when ``pty_host`` is on (claunch-xnnct), else None.

    Installed before the first spawn (restore_all). A host that cannot be
    reached is logged and the daemon spawns its sessions itself, as it does
    with the setting off: the setting must not be a way to start no sessions.
    """
    from . import host_code

    host_code.configure(bool(cfg.get("pty_host")) and sys.platform != "win32")
    if not cfg.get("pty_host"):
        _end_leftover_pty_host()
        return None
    if sys.platform == "win32":
        log.warning("pty_host is set but the PTY host runs on POSIX only; spawning in the daemon")
        return None
    from . import pty_backend
    from .ptyhost import client as ptyhost_client

    directory = paths.ptyhost_dir()
    deadline = time.monotonic() + HOST_BUSY_WAIT
    signalled = False
    while True:
        try:
            client = ptyhost_client.ensure_host(directory)
            break
        except Exception as exc:
            busy = "busy" in str(exc)
            # One client at a time: a predecessor that has not let go yet
            # holds the connection for a moment. Starting without the host
            # would relaunch every session it still runs (claunch-xnnct.1).
            if busy and time.monotonic() < deadline:
                time.sleep(0.2)
                continue
            # A live host that refused us (another protocol version, a token
            # that no longer matches) still runs its sessions. It is ended
            # by signal and a fresh one started, so the records relaunch
            # once; a busy one is somebody's, and is left alone.
            if not busy and not signalled and ptyhost_client.host_alive(directory) is not None:
                signalled = True
                if ptyhost_client.stop_refusing_host(directory):
                    continue
            stranded = ptyhost_client.host_alive(directory)
            if stranded is not None:
                pty_backend.mark_stranded(stranded)
                log.error(
                    "pty_host is set but PTY host pid %s could not be used or ended (%s); "
                    "the sessions it runs are not relaunched, and new ones spawn in the daemon",
                    stranded, exc,
                )
            else:
                log.error("pty_host is set but no PTY host could be reached (%s); "
                          "spawning in the daemon", exc)
            return None
    pty_backend.use_host(client)
    log.info("sessions run in PTY host pid %s (code %s)", client.host_pid, client.host_code)
    state = host_code.info()
    if state["stale"]:
        # Same protocol, other bytes: the host kept running across a keep
        # restart that changed its files (claunch-xnnct.3). It works; it
        # just runs the old code until a restart with --with-pty-host.
        log.warning(
            "PTY host pid %s runs code %s and this daemon loaded %s; it keeps "
            "running the older code until 'claunch daemon restart --with-pty-host'",
            client.host_pid, client.host_code or "(none reported)", state["daemon_code"],
        )
    return client


def _end_leftover_pty_host() -> None:
    """With ``pty_host`` off, end a host a previous daemon left running.

    A restart that kept sessions leaves the host running; when the setting
    was turned off in between, nothing would adopt them. They are ended here,
    before restore_all, which then relaunches each from its record (the
    ``--resume`` path) as it does for any session that ended across a restart.
    """
    if sys.platform == "win32":
        return
    from .ptyhost import client as ptyhost_client

    from . import pty_backend

    directory = paths.ptyhost_dir()
    if ptyhost_client.host_alive(directory) is None:
        return
    try:
        client = ptyhost_client.HostClient.connect(directory)
    except Exception as exc:
        log.warning("pty_host is off and a PTY host is still running, but it "
                    "could not be reached to end it (%s); ending it by signal", exc)
        client = None
    if client is not None:
        try:
            count = len(client.list())
            log.warning("pty_host is off: ending the PTY host a previous daemon left "
                        "running and its %d session(s); they are restored from their "
                        "records", count)
            client.end_all()
            client.shutdown_host()
            return
        except Exception:
            log.warning("could not end the leftover PTY host", exc_info=True)
            client.close()
    # Its sessions are relaunched from their records next; while the host
    # still runs them, that would run each conversation twice.
    if not ptyhost_client.stop_refusing_host(directory):
        pid = ptyhost_client.host_alive(directory)
        pty_backend.mark_stranded(pid)
        log.error("pty_host is off and PTY host pid %s could not be ended; the "
                  "sessions it runs are not relaunched", pid)


def _close_pty_host(client, mode: str) -> None:
    """Let go of the PTY host at teardown: leave it running ("keep") or shut
    it down with whatever it still holds ("full", "stop")."""
    if mode == "keep":
        client.close()
        return
    try:
        client.shutdown_host()
    except Exception:
        log.warning("the PTY host did not confirm its shutdown", exc_info=True)
        client.close()


async def _serve(host: str, port: int, cfg: dict, bound: Optional[dict] = None) -> int:
    _quiet_reset_errors(asyncio.get_running_loop())
    pty_host = _connect_pty_host(cfg)
    manager = SessionManager(
        idle_threshold=float(cfg["idle_threshold"]),
        scrollback=int(cfg["scrollback_lines"]),
        restore_default=bool(cfg["restore"]),
        focused_session_scheduling=bool(cfg["focused_session_scheduling"]),
        background_render_delay=float(cfg["background_render_delay"]),
        background_render_budget=float(cfg["background_render_budget_kib"]) * 1024,
        restore_stagger=float(cfg.get("restore_stagger_seconds") or 0.0),
    )
    # The fleet is restored *after* the port is bound (see below). Until
    # claunch-9akgh it ran here, and every client spent its whole duration
    # looking at a daemon that answered nothing -- measured on 2026-10-21 as
    # 11.7s of a 19.6s keep restart, all of it before the first byte of the
    # successor's HTTP surface existed.
    mesh_manager = MeshManager(manager)
    mesh_manager.load_all()

    token = runtime_state.load_or_create_token()
    relay_state = {"uplink": None}

    def _relay_state() -> dict:
        from .relay_uplink import unconfigured_state

        uplink = relay_state["uplink"]
        return unconfigured_state() if uplink is None else uplink.state()

    app = build_app(
        manager,
        token,
        started_at=time.monotonic(),
        mesh=mesh_manager,
        relay_state=_relay_state,
        gate_timeout=float(cfg["restart_approval_timeout"]),
        goto_timeout=float(cfg["goto_approval_timeout"]),
        shared_read_max_age=float(cfg["shared_read_max_age"] or 0),
    )

    runner = web.AppRunner(app, access_log=None)
    await runner.setup()
    # aiohttp's own connection count, which the WebSocket registry cannot
    # see: /api/connections reports both, so a reader can tell the sockets
    # apart from the ordinary request traffic sharing the same per-peer
    # budget (daemon/connections.py).
    app["http_server"] = runner.server
    # Default shutdown_timeout is 60s per lingering connection — far longer
    # than the restart flow's patience (stop waits 10s, the successor's lock
    # grace is 15s). Keep teardown well inside that budget.
    site = web.TCPSite(runner, host, port, shutdown_timeout=3.0)
    # What this boot still has to do, readable from the first request that
    # lands (GET /api/health and /api/daemon both carry it; claunch-9akgh).
    # Set before the socket accepts so no reader can see a bound port with
    # no answer to "is it done".
    app["boot"] = {
        "phase": "restoring",
        "done": 0,
        "total": 0,
        "started": time.monotonic(),
    }
    try:
        await site.start()
    except OSError as exc:
        log.error("cannot bind %s:%s: %s", host, port, exc)
        await runner.cleanup()
        return 1

    actual_port = port
    server = getattr(site, "_server", None)
    if server is not None and server.sockets:
        actual_port = server.sockets[0].getsockname()[1]
    # The snapshot is taken here because here is the boot: what this process
    # serves is the content of its source directory at import, and that stops
    # being readable as soon as anyone edits or checks out anything.
    # tools/deploy_check.py is the reader.
    runtime_state.write_daemon_json(
        host, actual_port, code=runtime_state.code_snapshot()
    )
    if bound is not None:
        bound["port"] = actual_port
    log.info("listening on http://%s:%s", host, actual_port)
    # Bring the fleet back now that the port answers. This is the boot's
    # longest stretch (11.7s for 33 adopted sessions on 2026-10-21,
    # claunch-9akgh); the async variant yields between records, so a page
    # that connects during it reads `boot: {phase: restoring, done, total}`
    # and can say what is happening instead of showing the previous daemon's
    # last frame. The restore stays before the clocks below on purpose:
    # every one of them drives sessions that must exist by then.
    def _boot_progress(done: int, total: int) -> None:
        app["boot"]["done"] = done
        app["boot"]["total"] = total

    failed = await manager.restore_all_async(progress=_boot_progress)
    app["boot"]["done"] = app["boot"]["total"]
    app["boot"]["phase"] = "ready"
    # build_app swept the retired records it could see, and it saw none: the
    # registry had not been read yet. The records restore_all just retired
    # owe the board the same sweep, or their in_progress issues keep
    # claiming a session that is not running (see the call in build_app).
    app["beads"].sessions_exited(manager.take_retired_for_sweep())
    if failed:
        # The cause of each one is already in the log — restore_all logs the
        # traceback where it catches. What belongs here is the set, said once,
        # which is the thing boot output is read for; a warning per name said
        # the same sentence a second time and no more than that.
        log.warning(
            "failed to restore %d session(s): %s",
            len(failed), ", ".join(failed),
        )
    restored = [s.sdef.name for s in manager.list() if not s.exited]
    retired = [s.sdef.name for s in manager.list()
               if s.exited and s.sdef.name not in manager.restore_queue]
    if restored:
        log.info("restored sessions: %s", ", ".join(restored))
    if retired:
        log.info(
            "kept %d exited session record(s), respawnable: %s",
            len(retired),
            ", ".join(retired),
        )
    # The same check `claunch daemon start/restart` prints: this daemon keeps
    # serving, and refuses sessions on these profiles with the same message.
    try:
        for problem in providers.compact_problems().values():
            log.error("%s -- sessions on this profile are refused", problem)
    except Exception:
        log.exception("could not check profiles for auto_compact_at")
    # Now that this daemon is announced, settle the account of the boot: read
    # (and clear) whatever asked for it, append this boot to the ledger the
    # next one will compare against, and turn both into debts. Done here
    # rather than at delivery time because the requests file is the *previous*
    # daemon's epitaph -- anything that appends to it after this point belongs
    # to the next restart, not this one.
    boot = runtime_state.read_daemon_json() or {}
    debts = restart_notice.note_boot(
        pid=boot.get("pid") or os.getpid(),
        started_at=boot.get("started_at") or "",
        version=boot.get("version") or "",
        port=actual_port,
        restored=list(manager.resumed_busy),
    )
    if debts:
        log.info(
            "restart notice: %d owed by this boot (%s)",
            len(debts),
            ", ".join(sorted({d["kind"] for d in debts})),
        )

    from .relay_settings import RelaySettings

    relay_settings = RelaySettings(actual_port, lambda pool: _wire_federation(mesh_manager, pool))
    await relay_settings.start()
    relay_state["uplink"] = relay_settings.pool
    app["relay_settings"] = relay_settings
    p2p_hub = _start_p2p(cfg, actual_port, token)
    app["p2p"] = p2p_hub
    mesh_manager.start()
    # Before any clock reads a v2 run: this daemon's run slots written before
    # the daemon qualifier move to <session>@<daemon>, once (claunch-9jmdz).
    from . import cflow2_slots

    try:
        await asyncio.to_thread(cflow2_slots.run_at_start, manager)
    except Exception:  # noqa: BLE001 - old slots stay readable where they are
        log.exception("cflow2 slots: start-time move failed; old slots stay in place")
    ask_clock = cflow_clock.AskClock()
    ask_clock.start()
    reminder_clock = session_reminder.SessionReminderService(manager, mesh_manager)
    reminder_clock.start()
    window_reminder_clock = window_mod.WindowReminderClock(manager, app["window"])
    window_reminder_clock.start()
    from .delayed_sweep import Clock as DelayedSweepClock
    delayed_sweep_clock = DelayedSweepClock(manager, window=app["window"], batch=True)
    delayed_sweep_clock.start()
    ping_clock = cflow_clock.StallPingClock(manager)
    ping_clock.start()
    window_clock = cflow_clock.WindowClock(manager)
    window_clock.start()
    timer_clock = cflow_clock.TimerClock(manager)
    timer_clock.start()
    checklist_clock = cflow_clock.ChecklistClock(manager)
    checklist_clock.start()
    restart_clock = cflow_clock.RestartClock(manager, boot_id=app["boot_id"])
    restart_clock.start()
    # What restart commands left running, kept current for the session
    # list and the terminal's process line (claunch-ihwel).
    procs_clock = session_procs.RefreshClock()
    procs_clock.start()
    trigger_clock = cflow_clock.TriggerClock(manager, mesh_manager)
    trigger_clock.start()
    round_clock = cflow_clock.RoundStartClock(manager)
    round_clock.start()
    event_clock = cflow_clock.RunEventClock(manager, mesh_manager)
    # A reviewer the workflow ends on its own is told to the person on the
    # notification channel, not asked (claunch-5aysr).
    event_clock.on_reviewer_ended = (
        lambda driver, reviewer, reason:
        app["kill_gate"].note_auto(driver, reviewer, reason)
    )
    # A crashed driver is reported to its parent from the exit itself, once
    # -- never re-read off state, which re-sent every dead run at each boot.
    manager.exit_hooks.append(event_clock.session_exited)
    event_clock.start()
    # cflow v2: waits, monitors, delegation budgets and the session
    # executors (daemon/cflow2_clock.py). Off by config -> v2 delegations
    # fall back to the next executor and nothing types into a v2 driver.
    cflow2_clock = None
    if cflow2_clock_mod.enabled():
        cflow2_clock = cflow2_clock_mod.Cflow2Clock(
            manager, poll=cflow2_clock_mod.poll_from(), mesh=mesh_manager
        )
        cflow2_clock.start()
        # A session's side runs end with it (docs/cflow-v2.md 2.9).
        manager.exit_hooks.append(cflow2_clock.session_exited)
    app["cflow2_clock"] = cflow2_clock
    # An ended session's pytest basetemps go with it (claunch-4zc51.2), and
    # the ones whose session ended while no daemon was up go now. Wired here
    # and not in create_app: tests build apps over registries of made-up
    # names, and this deletes real directories under C:/t.
    reaper = basetemp_reaper.BasetempReaper(basetemp_reaper.registry_view(manager))
    manager.exit_hooks.append(reaper.session_exited)
    reaper.reap_ended()
    # Published so the dashboard can report what these two are holding. Only
    # the two that type into a driving session on a timer: those are the ones
    # a person watching a terminal has no way to see coming, and the ones
    # whose silence is ambiguous — configured-and-armed and
    # configured-but-dead look identical from outside.
    app["session_reminder"] = reminder_clock
    # The dashboard's cflow timer readout consumes the cflow source's proxy
    # surface from the session-level service.  Keep this key for the existing
    # API while the runtime owner is published above under its real name.
    app["cflow_clocks"] = {"reminder": reminder_clock, "ping": ping_clock}
    # Last, and only now: the sessions restore brought back are alive but
    # nothing is driving them. Started after the server is up because a nudge
    # can send an agent straight back to the API it was using.
    resume_nudge = resume.ResumeNudge(
        manager,
        manager.resumed_busy,
        # The sessions whose conversation was not there to reopen. They get a
        # different message and the re-briefing, which is why the mesh manager
        # comes along — it is half of what a re-briefing is made of.
        blank=manager.resumed_blank,
        # Idle at the shutdown but writing shortly before it: a restart may
        # have killed the background command its agent was waiting on.
        recent=resume.recent_idle(manager.resumed_idle),
        # Operator sessions hear of every restart, whatever they were doing
        # (claunch-gi7g4): their runs park on purpose, so neither list above
        # reaches them.
        operators=resume.operator_sessions(
            [*manager.resumed_busy, *manager.resumed_idle, *manager.resumed_blank],
            operators=app.get("operators_v2"),
            mesh_mgr=mesh_manager,
        ),
        mesh_mgr=mesh_manager,
    )
    resume_nudge.start()

    def _delayed_woken(session) -> None:
        # Whether a codex restore came back blank is decided by its launch,
        # which for a delayed record ran after the nudge above was built.
        name = session.sdef.name
        if name in manager.resumed_blank:
            return
        if harness_mod.codex_restores_blank(session.sdef):
            manager.resumed_blank.append(name)
            resume_nudge.adopt_blank(name)

    manager.delayed_wake_hooks.append(_delayed_woken)
    # The sessions restore_all filed as delayed (claunch-0j6gb.2), started in
    # priority order a few seconds apart; any use starts one at once.
    queue = restore_queue.RestoreQueue(
        manager,
        roles_of=lambda name: [
            role for m in mesh_manager.meshes_for_session(name)
            for role in (m.get("roles") or [m.get("role")])
        ],
    )
    queue.start()
    # The dashboard reads its progress off the session list (claunch-v4uyt).
    app["restore_queue"] = queue
    # Started alongside it, and deliberately not merged into it: the nudge is
    # allowed to give up on a session that is working again, and this is not
    # (see restart_notice's module docstring). Where both are owed, whichever
    # lands first makes the other redundant -- and if that is this one, the
    # nudge reads the session as driven and stands down, which is right.
    restart_notice_task = restart_notice.RestartNotice(manager)
    restart_notice_task.start()
    # The listening socket can die under the daemon while everything else
    # lives on (see _listener_watchdog); this is what brings it back.
    site_box = {"site": site}
    listener_task = asyncio.ensure_future(
        _listener_watchdog(site_box, runner, host, actual_port)
    )
    # The saved model of a session drifts from the one it answers on the
    # moment somebody uses the harness's own model switch; this closes that.
    model_clock_task = asyncio.ensure_future(_model_reconcile_clock(manager))
    # Idle sessions give their program back and wake on use (claunch-5l35i).
    # Operator sessions always carry keep-alive (claunch-higqx): set once now
    # for the ones restore brought back, then before every hold pass.
    def _operator_keepalive() -> None:
        operator_keepalive.ensure(
            manager, operators=app.get("operators_v2"), mesh_mgr=mesh_manager
        )

    _operator_keepalive()
    hold_clock = hold_mod.HoldClock(manager, before=_operator_keepalive)
    hold_clock.start()

    try:
        await app["shutdown_event"].wait()
        log.info(
            "shutdown requested%s",
            " (restart)" if app["restart_requested"] else "",
        )
    except asyncio.CancelledError:
        log.info("cancelled; shutting down")
    finally:
        # Announce before anything is torn down: attached CLIs must learn this
        # is a daemon stop/restart (reattach later) before shutdown_all makes
        # their sessions look like programs that exited on their own.
        teardown = _Teardown()
        await teardown.step("notify_shutdown", notify_shutdown(app))
        await teardown.step("relay_settings", relay_settings.close())
        if p2p_hub is not None:
            await teardown.step("p2p", p2p_hub.stop())
        listener_task.cancel()
        try:
            await listener_task
        except (asyncio.CancelledError, Exception):
            pass
        model_clock_task.cancel()
        try:
            await model_clock_task
        except (asyncio.CancelledError, Exception):
            pass
        await teardown.step("hold_clock", hold_clock.stop())
        await teardown.step("restore_queue", queue.stop())
        await teardown.step("resume_nudge", resume_nudge.shutdown())
        # Undelivered debts stay on disk; shutdown only stops offering them.
        await teardown.step("restart_notice", restart_notice_task.shutdown())
        # Pending wind-downs are dropped, not finished: shutdown_all below
        # ends every session the daemon's way, and they come back on restart.
        await teardown.step("beads", app["beads"].cancel_all())
        await teardown.step("checklist_clock", checklist_clock.shutdown())
        await teardown.step("restart_clock", restart_clock.shutdown())
        await teardown.step("procs_clock", procs_clock.shutdown())
        await teardown.step("trigger_clock", trigger_clock.shutdown())
        await teardown.step("event_clock", event_clock.shutdown())
        if cflow2_clock is not None:
            await teardown.step("cflow2_clock", cflow2_clock.shutdown())
        await teardown.step("ping_clock", ping_clock.shutdown())
        await teardown.step("window_reminder_clock", window_reminder_clock.shutdown())
        await teardown.step("delayed_sweep_clock", delayed_sweep_clock.shutdown())
        await teardown.step("reminder_clock", reminder_clock.shutdown())
        await teardown.step("ask_clock", ask_clock.shutdown())
        await teardown.step("mesh", mesh_manager.shutdown())
        runtime_state.remove_daemon_json()
        # The restart mode (claunch-xnnct.1): "keep" leaves the sessions
        # running in the PTY host for the successor to adopt.
        mode = app.get("restart_mode") or "stop"
        if pty_host is None or pty_host.closed.is_set():
            mode = "stop" if mode == "keep" else mode
        log.info("teardown mode: %s", mode)
        await teardown.step("sessions", manager.shutdown_all(
            mode=mode, reason="restart" if app["restart_requested"] else "stop"))
        if pty_host is not None:
            await teardown.step("pty_host", asyncio.to_thread(_close_pty_host, pty_host, mode))
        # Sockets before the runner: aiohttp's server shutdown waits up to
        # its shutdown_timeout for open connections, and a terminal socket
        # is never idle-closed on its own. Closing them first (concurrently
        # and bounded -- see api.close_websockets) takes that wait out of
        # the restart; the shutdown hook closes them again, which is a no-op.
        await teardown.step("websockets", close_websockets(app))
        # The app's own on_shutdown/on_cleanup hooks run in here.
        await teardown.step("http runner", runner.cleanup())
        teardown.report()
    return RESTART_CODE if app["restart_requested"] else 0


class _Teardown:
    """Times the steps of a shutdown and names the slow ones in the log.

    The stretch between "shut down N live session(s)" and "spawning successor
    daemon" took anywhere from 3 s to 52 s across restarts with nothing in
    the log to say which step held it (claunch-t8bgj). Each step that runs
    past :data:`SLOW_TEARDOWN_STEP` is named with its time, and the total is
    said once at the end, so the next slow restart answers that from the log.
    """

    def __init__(self) -> None:
        self._started = time.perf_counter()
        self._slow: list = []

    async def step(self, name: str, awaitable) -> None:
        began = time.perf_counter()
        try:
            await awaitable
        finally:
            took = time.perf_counter() - began
            if took >= SLOW_TEARDOWN_STEP:
                self._slow.append((name, took))
                log.info("shutdown step %r took %.1fs", name, took)

    def report(self) -> None:
        log.info(
            "shutdown teardown took %.1fs%s",
            time.perf_counter() - self._started,
            "".join(f"; {n} {t:.1f}s" for n, t in self._slow),
        )


#: A shutdown step at least this long (seconds) is named in the log.
SLOW_TEARDOWN_STEP = 1.0


#: How often the watchdog looks at the listening socket.
LISTENER_POLL = 1.0
#: How often the saved model of each live session is checked against the
#: model it is actually answering on. Slow on purpose: the answer only
#: changes when a person switches model inside a harness, and a pass that
#: finds nothing writes nothing (see SessionManager.reconcile_models).
MODEL_RECONCILE_POLL = 60.0


def _listener_alive(site) -> bool:
    server = getattr(site, "_server", None)
    sockets = getattr(server, "sockets", None) or ()
    return any(s.fileno() != -1 for s in sockets)


async def _listener_watchdog(site_box: dict, runner, host: str, port: int) -> None:
    """Re-open the listening socket if asyncio closes it under us.

    On Windows the proactor's accept loop *closes the listening socket* when
    one accept fails with an OSError (``proactor_events._start_serving``):
    a client that gives up mid-handshake — a CLI health probe with a 3 s
    timeout against a busy daemon — completes AcceptEx with WinError 64, and
    from then on nothing listens on the port while the process, its
    sessions and its loop carry on. That was the 2026-09-11 14:27 outage:
    ``netstat`` showed no listener, the daemon showed 3.4 GB and a working
    loop. Seen as one asyncio error line in the log and nothing else.

    So: poll the socket, and when it is gone, start a new site on the same
    address. ``site_box`` is a one-key dict because the site object is
    replaced, and ``_serve``'s teardown must clean up the current one.
    """
    while True:
        await asyncio.sleep(LISTENER_POLL)
        site = site_box["site"]
        if _listener_alive(site):
            continue
        log.error(
            "listening socket on %s:%s is gone (asyncio closed it after a "
            "failed accept); re-opening", host, port,
        )
        try:
            await site.stop()
        except Exception:  # noqa: BLE001 — a dead site may refuse to stop
            pass
        new_site = web.TCPSite(runner, host, port, shutdown_timeout=3.0)
        try:
            await new_site.start()
        except OSError as exc:
            log.error("could not re-open %s:%s: %s (retrying)", host, port, exc)
            continue
        site_box["site"] = new_site
        log.info("listening again on http://%s:%s", host, port)


async def _model_reconcile_clock(manager) -> None:
    """Keep each session's saved model equal to the one it is answering on.

    Without this the definition keeps the creation-time choice for the whole
    life of the session, and the next daemon restart relaunches on it -- the
    ``/model`` switch silently undone, which is what this loop exists to stop
    (docs/session-model-persistence.md).

    ``persist`` reconciles too, and that is the one that matters at a restart;
    this timer is what keeps the dashboard and the saved definition agreeing
    in between, and what limits how much a crash (no clean shutdown, so no
    persist) can lose to one poll interval.
    """
    while True:
        await asyncio.sleep(MODEL_RECONCILE_POLL)
        try:
            changed = await asyncio.to_thread(manager.reconcile_models)
        except Exception:  # noqa: BLE001 -- a bad poll must not end the clock
            log.warning("model reconciliation pass failed", exc_info=True)
            continue
        if changed:
            # Only a pass that moved something pays for a write.
            try:
                await asyncio.to_thread(manager.persist)
            except Exception:  # noqa: BLE001
                log.warning(
                    "could not persist reconciled models for %s",
                    ", ".join(changed), exc_info=True,
                )


def _acquire_with_grace(
    lock: runtime_state.SingletonLock, *, timeout: float = 15.0, poll: float = 0.2
) -> bool:
    """Acquire the singleton lock, waiting out a predecessor that is draining.

    A restart stops the old daemon and spawns the new one right away, but the
    old process keeps holding the lock while its sessions shut down. Retry for
    a grace window instead of losing that race — while still exiting fast in
    the plain double-start case, where the lock holder is actually serving.

    A losing exit here is not necessarily a failure: this window is one
    successor's patience, and the caller that spawned it (``daemon_client``'s
    external stop+start flow, in particular) retries the whole spawn with its
    own backoff when the predecessor is still draining past it — see
    :func:`claude_launcher.daemon_client.restart`. Logged at INFO regardless,
    so a run that never comes back leaves a trail in ``daemon.log`` instead of
    the single line an exit used to leave.
    """
    deadline = time.monotonic() + timeout
    waited_log = False
    while True:
        if lock.acquire():
            return True
        if daemon_client.is_serving():
            return False
        if time.monotonic() >= deadline:
            log.info(
                "gave up waiting for the singleton lock after %.1fs — the "
                "predecessor still has not released it",
                timeout,
            )
            return False
        if not waited_log:
            waited_log = True
            log.info(
                "singleton lock is held and no daemon answers health checks "
                "— predecessor may still be draining; retrying for up to "
                "%.1fs",
                timeout,
            )
        time.sleep(poll)


def _start_uplink(actual_port: int):
    """Start the relay uplinks if any relay is configured.

    Every configured relay gets its own uplink and they run concurrently
    inside one :class:`~.relay_uplink.RelayPool`, so a daemon registered with
    a work relay and a home relay is reachable through both at once. The
    uplinks always dial the loopback address so the tunnel can't widen the
    daemon's own network exposure, regardless of the daemon's bind host.
    """
    from . import relay_uplink

    # A named instance sharing the config file with its siblings must not also
    # share their relay identity — suffix the default backend name so every
    # instance registers under its own directory entry.
    default_name = ""
    if paths.instance():
        import socket

        default_name = f"{socket.gethostname()}-{paths.instance()}"
    pool = relay_uplink.pool_from_config(
        store.relays_config(), local_host="127.0.0.1", local_port=actual_port,
        default_name=default_name,
    )
    if pool is None:
        return None, None
    for up in pool.uplinks:
        log.info("starting relay uplink %s → %s (backend %r)",
                 up.id, up.url, up.name)
    return pool, asyncio.ensure_future(pool.run())


def _start_p2p(cfg: dict, port: int, token: str):
    """The WebRTC hub for pages reached through a relay (claunch-mhzt4), or
    None when it is switched off or aiortc (``claunch[p2p]``) is absent --
    then the control socket never mentions P2P and nothing else changes."""
    from . import p2p

    if not cfg.get("p2p_enabled", True):
        return None
    if not p2p.available():
        log.info("P2P off: aiortc not installed (pip install 'claude-launcher[p2p]')")
        return None
    hub = p2p.Hub(
        local_port=port,
        token=token,
        stun=list(cfg.get("p2p_stun") or []),
        probe_stun=list(cfg.get("p2p_probe_stun") or []),
        predict=int(cfg.get("p2p_predict") or 0),
    )
    hub.warm()
    log.info("P2P on: stun %s, port prediction window %d", hub.stun, hub.predict)
    return hub


def _wire_federation(mesh_manager: MeshManager, uplink) -> None:
    """Give the mesh manager a peer transport riding the relay uplink(s).

    ``uplink`` is a :class:`~.relay_uplink.RelayPool` in the daemon and a lone
    :class:`~.relay_uplink.RelayUplink` in the tests that drive one directly;
    both expose ``name``/``connected``/``peer_http``/``peer_list``/
    ``carrier_url``, which is all this wiring needs.

    The transport is one JSON POST per call, bridged to the peer daemon's
    ``/peer/*`` endpoint through the relay (PEER_OPEN). Transport-level
    failures surface as PeerUnreachable (the mirror may then queue durably);
    an HTTP-level rejection surfaces as plain MeshError (never queued).
    """
    from . import peer_client, relay_uplink
    from .mesh import PeerUnreachable

    async def peer_call(machine: str, path: str, body: dict) -> dict:
        raw = peer_client.build_request(path, body, host=machine)
        try:
            resp = await uplink.peer_http(machine, raw)
        except relay_uplink.PeerError as exc:
            raise PeerUnreachable(str(exc)) from None
        try:
            status, payload = peer_client.parse_response(resp)
        except peer_client.PeerHttpError as exc:
            raise PeerUnreachable(
                f"peer {machine!r}: {exc}", status=exc.status
            ) from None
        if status >= 400:
            detail = payload.get("error") or f"HTTP {status}"
            raise MeshError(f"peer {machine!r} rejected {path}: {detail}")
        return payload

    async def peer_stream(machine: str, path: str, body: dict):
        """The same request, answered by a response that is read live —
        the shadow terminal (``daemon/shadow.py``). Returns the open
        :class:`~.relay_uplink.PeerBridge`; the caller parses and closes it."""
        raw = peer_client.build_request(path, body, host=machine)
        try:
            return await uplink.peer_open(machine, raw)
        except relay_uplink.PeerError as exc:
            raise PeerUnreachable(str(exc)) from None

    mesh_manager.machine = uplink.name
    mesh_manager.peer_transport = peer_call
    mesh_manager.peer_streamer = peer_stream
    mesh_manager.relay_connected = lambda: uplink.connected
    mesh_manager.relay_url_for = uplink.carrier_url
    mesh_manager.peer_lister = uplink.peer_list


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="claunch-daemon")
    parser.add_argument(
        "--foreground",
        action="store_true",
        help="log to stderr too (for debugging; the CLI starts the daemon detached)",
    )
    parser.add_argument(
        "--name",
        metavar="NAME",
        help="run as the named daemon instance (tmux -L style; also settable "
        "via the CLAUNCH_DAEMON env var)",
    )
    args = parser.parse_args(argv)
    # Out of the environment before anything can inherit it.
    successor_port = os.environ.pop(SUCCESSOR_PORT_ENV, "")
    if args.name:
        # Into the environment (not a variable) so every paths.instance()
        # call — and any child process — sees the same instance.
        os.environ[paths.INSTANCE_ENV] = paths.validate_instance(args.name)
    # A daemon started straight from a shell (``--name``/``CLAUNCH_DAEMON``
    # without the CLI) still gets its instance's home, config and port. One
    # the CLI auto-started inherited them already; apply() leaves that as is.
    # The port is the exception: a session's shell inherits the rest but not
    # the port, so the manifest's port is read again for the bind below.
    try:
        instance_manifest.apply()
        instance_manifest.check_pairing()
        manifest_port = instance_manifest.pinned_port()
    except instance_manifest.InstanceManifestError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2
    # No session's identity in the daemon's own environment, whatever shell
    # started it (claunch-2yiij): spawn_daemon removes it on the way, and
    # this covers every start that did not go through it (a daemon run by
    # hand, a predecessor on older code). After the manifest, which can move
    # the launcher home the profile-home check is measured against.
    scrubbed = session_env.scrub_in_place()
    # Nor its directory (claunch-2yiij.1): spawn_daemon starts the daemon in
    # daemon_client.daemon_cwd(), and a start that did not go through it is
    # moved there before anything reads os.getcwd().
    try:
        os.chdir(daemon_client.daemon_cwd())
    except OSError:
        pass
    _setup_logging(args.foreground)
    if scrubbed:
        log.info("removed session variables from the daemon environment: %s",
                 ", ".join(scrubbed))

    lock = runtime_state.SingletonLock()
    if not _acquire_with_grace(lock):
        log.info(
            "another daemon holds the lock (already serving, or a predecessor "
            "did not exit in time); exiting"
        )
        return 0

    cfg = store.daemon_config()
    host = str(cfg["host"])
    port = int(cfg["port"])
    if paths.instance():
        # Named instances share the config file with the default daemon, so
        # its fixed port would collide. They bind an ephemeral port instead
        # (their daemon.json is the discovery channel) unless one is pinned.
        port = int(
            os.environ.get("CLAUNCH_DAEMON_PORT") or successor_port or manifest_port or 0
        )
        log.info("daemon instance %r (state: %s)", paths.instance(), paths.daemon_dir())
    bound: dict = {}
    # asyncio.run() would release nothing until its close() has joined the
    # default executor's threads — and one of those may be sitting in a
    # subprocess.run() that cannot end until a *successor* daemon answers
    # (the cflow RestartClock running tools/restart_live.ps1, which calls
    # `claunch daemon restart`). Holding the singleton lock through that
    # join is a deadlock: the successor waits for the lock, the lock waits
    # for the executor, the executor waits for the successor (2026-09-11,
    # broken only by asyncio's 300s THREAD_JOIN_TIMEOUT). So the loop is
    # driven by hand: serve, release the lock, spawn the successor if one
    # was asked for, and only then tear the loop down.
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    try:
        try:
            code = loop.run_until_complete(_serve(host, port, cfg, bound))
        except KeyboardInterrupt:
            code = 0
        finally:
            lock.release()
        if code == RESTART_CODE:
            log.info("spawning successor daemon")
            daemon_client.spawn_daemon(
                _successor_env(bound.get("port")), reason="successor after restart"
            )
            code = 0
    finally:
        _close_loop(loop)
    return code


def _close_loop(loop: asyncio.AbstractEventLoop) -> None:
    """asyncio.run()'s teardown minus the executor join.

    Pending tasks are cancelled and async generators finalised as
    ``asyncio.run`` does. The default executor is *not* awaited: a worker
    still blocked in a subprocess (see ``main``) would hold this process
    for as long as that subprocess lives, and the interpreter's own exit
    joins those threads anyway. Nothing is lost by not waiting here — the
    lock is already free and the successor already spawned.
    """
    try:
        pending = [t for t in asyncio.all_tasks(loop) if not t.done()]
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        loop.run_until_complete(loop.shutdown_asyncgens())
    finally:
        asyncio.set_event_loop(None)
        loop.close()


def _successor_env(actual_port: Optional[int]) -> Optional[dict]:
    """The environment for the successor, or ``None`` to inherit ours.

    Its one job is pinning the successor to the port this daemon was
    serving on. Only named instances need it, and only they are affected:
    the default daemon's port is fixed in the config, so its successor
    rebinds the same one anyway, while an instance binds an ephemeral port
    and would come back somewhere else. That matters because the thing most
    likely to have asked for the restart is a browser on this address — a
    successor that moves is one the page cannot follow. An explicitly
    pinned port (``CLAUNCH_DAEMON_PORT`` already set) is left as it is.

    Returned as a copy rather than set on ``os.environ``: a variable poked
    into this process on the way out is still there for everything else
    sharing it, which in a test run is every later test. And carried in
    :data:`SUCCESSOR_PORT_ENV`, which the successor removes as it starts,
    rather than in ``CLAUNCH_DAEMON_PORT``, which would stay in the
    successor's environment and pass to its sessions.
    """
    if not actual_port or not paths.instance():
        return None
    if os.environ.get("CLAUNCH_DAEMON_PORT"):
        return None
    return {**os.environ, SUCCESSOR_PORT_ENV: str(actual_port)}


if __name__ == "__main__":
    sys.exit(main())
