"""Watch other sessions' cflow runs (``claunch-ijg51.1``).

Two halves, one record:

- **read** — ``runs`` answers "where is each run now" by reading the slots
  through :mod:..cflow_watch, and with ``since`` it answers "what moved since
  this cursor" as well. Read-only, allowed for a target in the caller's
  subtree or one it is connected to in the mesh (design D1).
- **subscribe** — a subscription is one row in the watcher's own
  ``loops.json`` (``"watch"`` beside ``"loops"``): ``{target, kinds, cursor,
  since}``. :meth:`WatchBoard.tick`, run by the daemon clock, reads each
  watched run from its cursor and types one compressed fyi block into the
  watcher. The cursor is saved only when the queue reports the block was
  actually delivered (``on_done``), not when it accepted it — acceptance is
  not a delivery receipt — and ``subscriptions`` can always backfill from the
  stored cursor, so a lost block is recoverable rather than gone.

Delivery goes through ``Session.queue_delivery`` — the same deferred queue QA
answers and cflow notices use, which waits for a free terminal. It is
deliberately not the mesh send path: mesh mail is addressed and accounted
(``owed``); a watch line is a status signal, and its durable copy is the
journal the watcher can always pull again with ``runs``.

A slot is read only when its files moved (``cflow_watch.token``): a watch
pass over idle runs costs a stat per run, not a checkpoint decode, and a tick
whose events all fell outside ``kinds`` still stores the cursor it read to.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Dict, List, Optional, Set, Tuple

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
        #: Targets whose block the queue accepted but has not delivered yet.
        #: Their cursor must not move and they must not be re-read, or the same
        #: events would be queued again behind the first block.
        self._inflight: Dict[str, Set[str]] = {}

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
    def subscriptions(self, watcher: str, *, backfill: bool = False) -> List[dict]:
        """The rows, and with ``backfill`` what each one's next delivery carries.

        Backfill does not move the stored cursor: it shows the watcher what it
        has not been delivered yet, and the tick still delivers it (or the
        watcher unsubscribes if it already has what it needs).
        """
        rows = loops.watch_rows(watcher)
        if not backfill:
            return rows
        out: List[dict] = []
        for row in rows:
            try:
                cwd, scope = _scope_for(self.manager, str(row.get("target") or ""))
                found = watch_core.reading(cwd, scope, str(row.get("cursor") or ""),
                                           row.get("kinds"))
            except Exception as exc:  # noqa: BLE001 - one unreadable row
                out.append({**row, "events": [], "error": str(exc)})
                continue
            out.append({
                **row,
                "cursor": found["cursor"],
                "status": found["status"],
                "events": found["events"],
                "present": found["present"],
            })
        return out

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
            "token": watch_core.token(cwd, scope),
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
        inflight = self._inflight.get(watcher)
        if inflight is not None:
            inflight.discard(target)
        return True

    # -- read ------------------------------------------------------------
    def runs(self, watcher: str, targets: List[str], since: Optional[str] = None) -> dict:
        """Where each target's run stands, and with ``since`` what it did after."""
        out, refused = [], []
        for target in targets:
            if not self.may_watch(watcher, target):
                refused.append({"session": target, "reason": "not in your subtree or mesh"})
                continue
            try:
                cwd, scope = _scope_for(self.manager, target)
                found = watch_core.slot(cwd, scope)
                if found is None:
                    refused.append({"session": target, "reason": "no live cflow run"})
                    continue
                if since:
                    read = watch_core.reading(cwd, scope, since)
                    found = {**found, "events": read["events"], "cursor": read["cursor"]}
            except Exception as exc:  # noqa: BLE001 - one unreadable run
                refused.append({"session": target, "reason": str(exc)})
                continue
            out.append(found)
        return {"runs": out, "refused": refused}

    # -- delivery --------------------------------------------------------
    def _lines(self, watcher: str, rows: List[dict]) -> Tuple[List[str], List[dict], Set[str]]:
        """What moved, the rows to store, and the targets whose line is in them.

        The cursor is always taken from the read, even when every event fell
        outside ``kinds``: dropping it made a quiet run re-read its whole
        checkpoint on every tick. A row whose files did not change since the
        last read is not read again at all.
        """
        lines: List[str] = []
        updated: List[dict] = []
        sent: Set[str] = set()
        inflight = self._inflight.get(watcher) or set()
        for row in rows:
            target = str(row.get("target") or "")
            if len(updated) >= MAX_TARGETS_PER_TICK:
                updated.append(row)
                continue
            if target in inflight:
                updated.append(row)
                continue
            try:
                cwd, scope = _scope_for(self.manager, target)
                watched = self.manager.get(target)
            except Exception as exc:  # noqa: BLE001 - the session is gone
                if not row.get("missing"):
                    lines.append(
                        f"{target} is gone: the session no longer exists — its run "
                        f"stopped without reporting ({type(exc).__name__})"
                    )
                    sent.add(target)
                    updated.append({**row, "missing": True, "cursor": row.get("cursor") or ""})
                else:
                    updated.append(row)
                continue

            # A session's exit is not a file change: it has to be seen before
            # the token shortcut, or a run that stopped moving would never
            # report that the session behind it went away.
            if getattr(watched, "exited", False):
                if not row.get("exited"):
                    lines.append(
                        f"{target} session exited: it stopped without finishing its run "
                        "(read `runs` for where the run stands)"
                    )
                    sent.add(target)
                    updated.append({**row, "exited": True})
                else:
                    updated.append(row)
                continue
            if row.get("exited"):
                row = {**row, "exited": False}

            token = watch_core.token(cwd, scope)
            if (row.get("token") and row.get("token") == token
                    and row.get("cursor") and not row.get("more")):
                if row.get("missing"):
                    row = {**row, "missing": False}
                updated.append(row)
                continue

            read = watch_core.reading(cwd, scope, str(row.get("cursor") or ""),
                                      row.get("kinds"))
            new_row = {**row, "cursor": read["cursor"], "token": read["token"],
                       "missing": False,
                       "more": len(read["events"]) >= watch_core.BATCH_LIMIT}
            if not read["present"]:
                if row.get("ended") != "gone":
                    lines.append(f"{target} run is gone: the slot holds no live run")
                    sent.add(target)
                    new_row["ended"] = "gone"
                updated.append(new_row)
                continue
            for event in read["events"]:
                detail = f" {event['detail']}" if event.get("detail") else ""
                at = event.get("at") or ""
                lines.append(f"{target} [{at}] {event.get('event')}{detail}")
            if read["events"]:
                sent.add(target)
            if len(read["events"]) >= watch_core.BATCH_LIMIT:
                lines.append(f"{target} (more events pending; the next tick carries them)")

            # A gate the journal never names: v1 records no event on entering
            # a gate, and v2 approvals live in state. The owed signature moves
            # when a person becomes owed, so watch that.
            owed = "; ".join(read["owed"])
            if owed and owed != (row.get("owed") or ""):
                lines.append(f"{target} gate: {owed}")
                sent.add(target)
                new_row["owed"] = owed
            elif not owed:
                new_row["owed"] = ""

            if read["status"] in watch_core.TERMINAL_STATUSES and row.get("ended") != read["run"]:
                lines.append(
                    f"{target} run ended: run {read['run']} status {read['status']}"
                )
                sent.add(target)
                new_row["ended"] = read["run"]
            new_row["exited"] = False
            updated.append(new_row)
        return lines, updated, sent

    def _settle(self, watcher: str, updated: List[dict], sent: Set[str],
                delivered: bool) -> None:
        """Store what the delivery proved; keep the rest for the next tick.

        Only the rows whose line was in the block are taken from ``updated``;
        every other row is read back as it stands now, because a tick may have
        advanced it while this block was waiting for a terminal.
        """
        self._inflight.get(watcher, set()).difference_update(sent)
        fresh = {r.get("target"): r for r in updated}
        kept = []
        for row in self.subscriptions(watcher):
            target = row.get("target")
            candidate = fresh.get(target)
            if candidate is None:
                kept.append(row)
            elif target in sent:
                kept.append(candidate if delivered else row)
            else:
                kept.append(candidate)
        self._save(watcher, kept)
        if not delivered:
            log.info("cflow watch: delivery to %r not confirmed; cursor kept", watcher)

    async def tick(self) -> None:
        """One pass: deliver what moved, advance the cursors that landed."""
        for session in list(self.manager.list()):
            watcher = session.sdef.name
            if getattr(session, "exited", False):
                continue
            rows = self.subscriptions(watcher)
            if not rows:
                continue
            lines, updated, sent = self._lines(watcher, rows)
            if not lines:
                if updated != rows:
                    self._save(watcher, updated)
                continue
            block = watch_core.compose_block(watcher, lines)
            # A held session stores the block in its durable wake queue, which
            # has no completion callback of its own: acceptance is the promise.
            restore = getattr(session, "message_restore_pending", None)
            restoring = bool(callable(restore) and restore(watcher))
            held = bool(getattr(session, "held_at", None)) or restoring
            settled = sent
            if held:
                try:
                    accepted = session.queue_delivery(block)
                except Exception:  # noqa: BLE001
                    log.exception("cflow watch delivery to %r failed", watcher)
                    accepted = False
                if accepted:
                    self._save(watcher, updated)
                else:
                    log.info("cflow watch: wake queue refused %r; cursor kept", watcher)
                await asyncio.sleep(0)
                continue

            def done(ok: bool, watcher: str = watcher, updated: List[dict] = updated,
                     sent: Set[str] = settled) -> None:
                try:
                    self._settle(watcher, updated, sent, ok)
                except Exception:  # noqa: BLE001 - a failed save is a retry
                    log.exception("cflow watch: settling %r failed", watcher)

            try:
                self._inflight.setdefault(watcher, set()).update(sent)
                accepted = session.queue_delivery(block, on_done=done)
            except TypeError:
                # A double (tests) or an older session object without the
                # callback: the return value is all we get.
                self._inflight.get(watcher, set()).difference_update(sent)
                accepted = session.queue_delivery(block)
                done(bool(accepted))
            except Exception:  # noqa: BLE001 - a failed delivery is a debt
                log.exception("cflow watch delivery to %r failed", watcher)
                self._inflight.get(watcher, set()).difference_update(sent)
                accepted = False
            if not accepted:
                self._inflight.get(watcher, set()).difference_update(sent)
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
        since = str(request.query.get("since") or "").strip() or None
        return web.json_response(
            await asyncio.to_thread(self.runs, watcher, targets, since)
        )

    async def h_subscriptions(self, request: web.Request) -> web.Response:
        watcher = _session(request)
        if not watcher:
            return _error(400, "session is required")
        backfill = str(request.query.get("backfill") or "1").strip() not in ("0", "false", "no")
        rows = await asyncio.to_thread(self.subscriptions, watcher, backfill=backfill)
        return web.json_response({"subscriptions": rows})

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
