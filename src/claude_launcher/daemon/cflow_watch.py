"""Watch other sessions' cflow runs (``claunch-ijg51.1``).

Two halves, one record:

- **read** — ``runs`` answers "where is each run now" by reading the slots
  through :mod:..cflow_watch. Read-only, allowed for a target in the
  caller's subtree or one it is connected to in the mesh (design D1).
- **subscribe** — a subscription is one row in the watcher's own
  ``loops.json`` (``"watch"`` beside ``"loops"``): ``{target, kinds,
  cursor, since}``. :meth:`WatchBoard.tick`, run by the daemon clock, reads
  each watched run's journal from its cursor and types one compressed fyi
  block into the watcher. Cursors advance only after the queue accepts the
  block, so a refused delivery is retried on the next tick.

Delivery goes through ``Session.queue_delivery`` — the same deferred queue QA
answers and cflow notices use, which waits for a free terminal. It is
deliberately not the mesh send path: mesh mail is addressed and accounted
(``owed``); a watch line is a status signal, and its durable copy is the
journal the watcher can always pull again with ``runs``.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Dict, List, Optional, Tuple

from aiohttp import web

from .. import cflow_watch as watch_core
from ..cflow2 import store as v2_store
from . import loops

log = logging.getLogger(__name__)

#: How often the board reads watched journals. The v2 journal is written by
#: the run's own commits, so ten seconds keeps a gate/report well inside the
#: pace a person can read; the per-watcher block carries everything that
#: moved in between, so a slower cadence costs latency, not events.
INTERVAL = 10.0

#: Subscriptions per watcher, and watched rows per delivery. Both exist so one
#: watcher cannot turn the daemon into a broadcast amplifier.
MAX_SUBSCRIPTIONS = 16
MAX_TARGETS_PER_TICK = 8


class WatchError(Exception):
    def __init__(self, message: str, status: int = 400) -> None:
        super().__init__(message)
        self.status = status


def _scope_for(manager, target: str) -> Tuple[str, str]:
    """``(cwd, scope)`` of the run ``target`` drives, in its own directory.

    The v2 scope carries the daemon id (``<session>@<daemon>``); the v1 slot
    is keyed by the bare session name. A side run (``+stack``) and a nested
    delegation run (``~d1``) belong to the main run and are never the thing a
    watcher subscribed to.
    """
    session = manager.get(target)
    cwd = str(getattr(session.sdef, "cwd", "") or "")
    if not cwd:
        raise WatchError(f"{target} has no working directory")
    for scope in v2_store.list_scopes(cwd):
        if v2_store.side_of(scope) or "~" in scope:
            continue
        if v2_store.session_of(scope) == target:
            return cwd, scope
    return cwd, target


class WatchBoard:
    """Subscriptions and their delivery. One per daemon."""

    def __init__(self, manager, mesh=None, *, interval: float = INTERVAL) -> None:
        self.manager = manager
        self.mesh = mesh
        self.interval = interval
        self._task: Optional[asyncio.Task] = None
        #: Subscriptions that failed to deliver and keep their cursor.
        self._debt: Dict[str, List[dict]] = {}

    # -- permissions -----------------------------------------------------
    def may_watch(self, watcher: str, target: str) -> bool:
        def descendant(name: str) -> bool:
            return name in self.manager.descendants(watcher)

        def connected(name: str) -> bool:
            if self.mesh is None:
                return False
            for meta in self.mesh.meshes_for_session(watcher):
                mesh = self.mesh.get(meta["mesh"])
                if mesh is None:
                    continue
                if mesh.connected(watcher, name):
                    return True
            return False

        return watch_core.may_watch(descendant, connected, target, watcher)

    # -- record ----------------------------------------------------------
    def subscriptions(self, watcher: str) -> List[dict]:
        return loops.watch_rows(watcher)

    def _save(self, watcher: str, rows: List[dict]) -> None:
        loops.save_watch(watcher, rows)

    def subscribe(self, watcher: str, target: str,
                  kinds=None) -> dict:
        if not self.may_watch(watcher, target):
            raise WatchError(
                f"{watcher} may not watch {target}: only a session in its "
                "subtree, or one it is connected to in the mesh",
                status=403,
            )
        rows = [r for r in self.subscriptions(watcher) if r.get("target") != target]
        if len(rows) >= MAX_SUBSCRIPTIONS:
            raise WatchError(
                f"{watcher} already watches {len(rows)} runs (max {MAX_SUBSCRIPTIONS})",
                status=409,
            )
        cwd, scope = _scope_for(self.manager, target)
        found = watch_core.slot(cwd, scope)
        row = {
            "target": target,
            "kinds": list(kinds) if kinds else list(watch_core.DEFAULT_KINDS),
            "cursor": (found or {}).get("cursor") or "",
            "since": time.time(),
        }
        rows.append(row)
        self._save(watcher, rows)
        return row

    def unsubscribe(self, watcher: str, target: str) -> bool:
        rows = self.subscriptions(watcher)
        kept = [r for r in rows if r.get("target") != target]
        if len(kept) == len(rows):
            return False
        self._save(watcher, kept)
        self._debt.pop(watcher, None)
        return True

    # -- read ------------------------------------------------------------
    def runs(self, watcher: str, targets: List[str]) -> dict:
        out, refused = [], []
        for target in targets:
            if not self.may_watch(watcher, target):
                refused.append({"session": target, "reason": "not in your subtree or mesh"})
                continue
            try:
                cwd, scope = _scope_for(self.manager, target)
                found = watch_core.slot(cwd, scope)
            except Exception as exc:  # noqa: BLE001 - one unreadable run
                refused.append({"session": target, "reason": str(exc)})
                continue
            if found is None:
                refused.append({"session": target, "reason": "no live cflow run"})
                continue
            out.append(found)
        return {"runs": out, "refused": refused}

    # -- delivery --------------------------------------------------------
    def _lines(self, watcher: str, rows: List[dict]) -> Tuple[List[str], List[dict]]:
        """Collect what moved, and the rows with their new cursors.

        One line per event: ``<target> [<at>] <event> <detail>``. A target
        that produced more than ``BATCH_LIMIT`` events keeps the cursor just
        past what was sent and says so, so the next tick carries the rest
        instead of dropping it.
        """
        lines: List[str] = []
        updated: List[dict] = []
        for row in rows:
            if len(updated) >= MAX_TARGETS_PER_TICK:
                updated.append(row)
                continue
            target = str(row.get("target") or "")
            try:
                cwd, scope = _scope_for(self.manager, target)
                events, cursor = watch_core.events_since(
                    cwd, scope, str(row.get("cursor") or ""), row.get("kinds")
                )
            except Exception as exc:  # noqa: BLE001
                log.debug("cflow watch: %r unreadable: %s", target, exc)
                updated.append(row)
                continue
            if not events:
                updated.append(row)
                continue
            for event in events:
                detail = f" {event['detail']}" if event.get("detail") else ""
                at = event.get("at") or ""
                lines.append(f"{target} [{at}] {event.get('event')}{detail}")
            updated.append({**row, "cursor": cursor})
            if len(events) >= watch_core.BATCH_LIMIT:
                lines.append(f"{target} (more events pending; the next tick carries them)")
        return lines, updated

    async def tick(self) -> None:
        """One pass: deliver what moved, advance the cursors that landed."""
        for session in list(self.manager.list()):
            watcher = session.sdef.name
            if getattr(session, "exited", False):
                continue
            rows = self.subscriptions(watcher)
            if not rows:
                continue
            lines, updated = self._lines(watcher, rows)
            if not lines:
                if updated != rows:
                    self._save(watcher, updated)
                continue
            block = watch_core.compose_block(watcher, lines)
            try:
                accepted = session.queue_delivery(block)
            except Exception:  # noqa: BLE001 - a failed delivery is a debt
                log.exception("cflow watch delivery to %r failed", watcher)
                accepted = False
            if accepted:
                self._save(watcher, updated)
            else:
                log.info("cflow watch: delivery to %r not accepted; cursor kept", watcher)
            await asyncio.sleep(0)

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.interval)
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("cflow watch tick failed")

    # -- HTTP ------------------------------------------------------------
    # The handlers reach this board through ``app["cflow_watch"]`` (set by the
    # daemon's main) so this module never imports ``daemon.api`` — that module
    # imports this one to register the routes.
    async def h_runs(self, request: web.Request) -> web.Response:
        watcher = _session(request)
        if not watcher:
            return _error(400, "session is required (this session's name)")
        targets = [t.strip() for t in str(request.query.get("targets") or "").split(",") if t.strip()]
        if not targets:
            targets = [
                row.get("target") for row in self.subscriptions(watcher) if row.get("target")
            ]
        if not targets:
            return _error(400, "targets is required when nothing is subscribed")
        return web.json_response(await asyncio.to_thread(self.runs, watcher, targets))

    async def h_subscriptions(self, request: web.Request) -> web.Response:
        watcher = _session(request)
        if not watcher:
            return _error(400, "session is required")
        return web.json_response({"subscriptions": self.subscriptions(watcher)})

    async def h_subscribe(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        watcher = str(body.get("session") or "")
        target = str(body.get("target") or "")
        kinds = body.get("kinds")
        if not watcher or not target:
            return _error(400, "session and target are required")
        if kinds is not None and not isinstance(kinds, list):
            return _error(400, "kinds must be a list of event names")
        try:
            row = await asyncio.to_thread(self.subscribe, watcher, target, kinds)
        except WatchError as exc:
            return _error(exc.status, str(exc))
        except Exception as exc:  # noqa: BLE001
            return _error(400, str(exc))
        return web.json_response({"subscription": row})

    async def h_unsubscribe(self, request: web.Request) -> web.Response:
        body = await request.json() if request.can_read_body else {}
        watcher = str(body.get("session") or "")
        target = str(body.get("target") or "")
        if not watcher or not target:
            return _error(400, "session and target are required")
        removed = await asyncio.to_thread(self.unsubscribe, watcher, target)
        return web.json_response({"removed": removed})


def _error(status: int, message: str) -> web.Response:
    return web.json_response({"error": message}, status=status)


def _session(request: web.Request) -> str:
    return str(request.query.get("session") or "").strip()
