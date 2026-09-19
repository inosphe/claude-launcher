"""Session-level reminders assembled from independent state sources.

The daemon used to let cflow own the only reminder delivery.  A stalled
position produced a fenced cflow block and session-level facts (role, opening
task ids, mesh mail and children) were appended to its tail.  That made those
facts depend on a run existing and left the role line visually subordinate to
the step that happened to trigger it.

This module owns the delivery now.  Cflow remains one source and keeps the
position key, no-progress policy, full/repeat decision and ``awaits`` probe in
``cflow_clock.CflowReminderSource``.  Role is another source with its own key
and interval.  Sources that become due together are rendered as peer sections
inside one terminal delivery, and each source advances only after that
delivery succeeds.
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
import time
from typing import Dict, List, Optional, Tuple

from .. import digests, store
from ..cflow import engine as cflow_engine
from . import cflow_clock, mesh_roles, rebrief, score_goal
from .session import STATUS_BUSY

log = logging.getLogger("claunch.daemon.session-reminder")


def role_reminder_policy(cfg: dict) -> Tuple[bool, float]:
    """Effective machine policy for the role source.

    Role recovery is session state, so it has its own switch and interval.
    The same lower bound as cflow reminders prevents a bad live config edit
    from typing into every role-bearing terminal every few seconds.
    """
    enabled = bool(cfg.get("role_reminder", True))
    interval = float(cfg.get("role_reminder_interval") or 0)
    if interval > 0:
        interval = max(interval, cflow_engine.REMINDER_MIN_INTERVAL)
    return enabled and interval > 0, interval


def role_entries(name: str, manager, mesh_mgr, *, session=None) -> List[dict]:
    """Resolved roles currently held by one local session.

    Mesh membership is the authoritative role.  ``SessionDef.role`` is read
    only as a compatibility fallback for sessions created before role
    delivery moved out of Claude's appended system prompt.
    """
    out: List[dict] = []
    if mesh_mgr is not None:
        try:
            rows = mesh_mgr.meshes_for_session(name)
        except Exception:  # noqa: BLE001 - one roster read must not stop a tick
            rows = []
        for row in rows:
            try:
                mesh = mesh_mgr.get(row["mesh"])
                member = mesh_mgr.member_for_session(mesh, name)
                if member is None:
                    continue
                role = mesh.roleset.get(member.role)
                if role is None:
                    continue
                stance = (role.stance or "").strip()
                out.append(
                    {
                        "mesh": mesh.name,
                        "name": role.name,
                        "stance": stance,
                        "digest": digests.text_digest(stance),
                        "cflow_reminder": (role.cflow_reminder or "").strip(),
                    }
                )
            except Exception:  # noqa: BLE001 - a changing mesh is one omitted role
                continue
    if out:
        return sorted(out, key=lambda e: (e["mesh"], e["name"]))

    try:
        current = session if session is not None else manager.get(name)
        raw = (current.sdef.role or "").strip()
    except Exception:  # noqa: BLE001 - an unknown session has no role
        raw = ""
    role = mesh_roles.resolve().get(raw) if raw else None
    if role is not None:
        stance = (role.stance or "").strip()
        out.append(
            {
                "mesh": "",
                "name": role.name,
                "stance": stance,
                "digest": digests.text_digest(stance),
                "cflow_reminder": (role.cflow_reminder or "").strip(),
            }
        )
    return out


def role_key(entries: List[dict]) -> tuple:
    """The role source's stable position key."""
    return tuple((e["mesh"], e["name"], e["digest"]) for e in entries)


def role_section(entries: List[dict], *, cflow_guidance: bool) -> List[str]:
    """The Role section, with stance recovery and optional step guidance."""
    lines: List[str] = []
    for index, entry in enumerate(entries):
        if index:
            lines.append("")
        where = f" on {entry['mesh']}" if entry["mesh"] else ""
        lines.append(f"role: {entry['name']}{where}")
        ident = entry.get("digest") or ""
        if ident:
            lines.extend(
                [
                    f"stance text id: {ident}",
                    "recovery: find that id attached to the stance text in this "
                    "conversation. If it is absent, call the mesh 'rebrief' tool "
                    f"with id {ident}; this id line alone is not the stance.",
                ]
            )
        if cflow_guidance and entry.get("cflow_reminder"):
            lines.append(f"at this cflow position: {entry['cflow_reminder']}")
    return lines


def _cflow_section(block: str) -> List[str]:
    """Remove a cflow block's outer fence and demote its header to metadata."""
    rows = str(block or "").splitlines()
    if rows and rows[0] == "---":
        rows = rows[1:]
    if rows and rows[-1] == "---":
        rows = rows[:-1]
    if rows and rows[0].startswith("# claunch cflow: reminder -- "):
        notice = rows.pop(0).split(" -- ", 1)[1]
        rows.insert(0, f"reminder: {notice}")
    return rows


def _situation_section(lines: List[str]) -> List[str]:
    """Drop the old inline divider; the Situation heading replaces it."""
    rows = list(lines)
    if rows and rows[0].startswith("-- around you right now"):
        rows.pop(0)
    return rows


def situation_lines(name: str, manager, mesh_mgr, open_asks: int = 0) -> List[str]:
    """Mutable state around a session, recomputed at delivery time."""
    lines: List[str] = []
    try:
        sdef = manager.get(name).sdef
    except Exception:  # noqa: BLE001 - raced an exit; no session to describe
        return lines
    if open_asks:
        lines.append(
            f"asks: {open_asks} delegated decision(s) from other runs await "
            "your answer -- their workflows are stopped on it. The cflow "
            "'asks' tool serves them, 'answer' closes them."
        )
    if mesh_mgr is not None:
        try:
            for row in mesh_mgr.meshes_for_session(name):
                mesh = mesh_mgr.get(row["mesh"])
                member = mesh_mgr.member_for_session(mesh, name)
                if member is None:
                    continue
                owed = mesh.owed(member.handle)
                if owed:
                    lines.append(
                        f"owed: {len(owed)} delivered message(s) on mesh "
                        f"{mesh.name} still await your reply -- to the "
                        "senders, silence is silence. 'claunch mesh history "
                        f"{mesh.name} -n 30' shows them."
                    )
        except Exception:  # noqa: BLE001 - one changing mesh omits that row
            pass
    try:
        live = manager.live_children(name)
    except Exception:  # noqa: BLE001
        live = []
    if live:
        lines.append(
            f"children: {', '.join(live)} still running and still reporting "
            "to you -- a child holding a finished result keeps holding it "
            "until you ask."
        )
    if sdef.parent:
        try:
            if manager.get(sdef.parent).exited:
                lines.append(
                    f"parent: {sdef.parent} has exited -- whatever you were "
                    "going to report to it has nowhere to go. Say so in this "
                    "run's next report rather than reporting into the void."
                )
        except Exception:  # noqa: BLE001 - no record is not an exited record
            pass
    if lines:
        lines.insert(
            0,
            "-- around you right now (this part changes; it is stated in "
            "full because there is no id that could stay true for it) --",
        )
    return lines


def context_id_lines(name: str, manager, mesh_mgr) -> List[str]:
    """IDs for stable non-role session text, currently the opening task."""
    try:
        ids = [
            (ident, kind)
            for ident, kind in rebrief.given_ids(
                name, manager=manager, mesh_mgr=mesh_mgr
            )
            if not kind.startswith("stance (")
        ]
    except Exception:  # noqa: BLE001 - context decoration never sinks delivery
        ids = []
    if not ids:
        return []
    named = "; ".join(f"{ident} ({kind})" for ident, kind in ids)
    return [
        f"session text ids: {named}",
        "recovery: an id counts only where it is attached to its full text. "
        "If the attached text is absent from this conversation, call the "
        "mesh 'rebrief' tool with that id.",
    ]


def reminder_block(
    name: str,
    *,
    roles: List[dict],
    cflow: str = "",
    situation: Optional[List[str]] = None,
    context: Optional[List[str]] = None,
    cflow_guidance: bool = False,
    goal: str = "",
) -> str:
    """Render one session reminder with peer Role and Cflow sections."""
    sections: List[Tuple[str, List[str]]] = []
    if goal:
        sections.append(("Score goal", [goal]))
    role_lines = role_section(roles, cflow_guidance=cflow_guidance)
    if role_lines:
        sections.append(("Role", role_lines))
    cflow_lines = _cflow_section(cflow)
    if cflow_lines:
        sections.append(("Cflow", cflow_lines))
    context_lines = list(context or [])
    if context_lines:
        sections.append(("Context", context_lines))
    situation_lines = _situation_section(list(situation or []))
    if situation_lines:
        sections.append(("Situation", situation_lines))

    lines = [
        "---",
        "# claunch session: reminder -- machine-generated",
        f"session: {name}",
    ]
    for title, body in sections:
        lines.extend(["", f"## {title}", *body])
    lines.append("---")
    return "\n".join(lines)


class SessionReminderService:
    """Coordinate cflow and role reminders into one delivery per session."""

    def __init__(self, manager, mesh=None, *, poll: float = cflow_clock.REMINDER_POLL):
        self.manager = manager
        self.mesh = mesh
        self.poll = poll
        self.cflow = cflow_clock.CflowReminderSource(manager)
        # Compatibility for callers and tests that inspect the former clock's
        # table directly.  The table still belongs to the cflow source.
        self._seen = self.cflow._seen
        self._roles: Dict[str, dict] = {}
        self._goals: Dict[str, dict] = {}
        self._task: Optional[asyncio.Task] = None

    def start(self) -> None:
        if self._task is None:
            self._task = asyncio.get_running_loop().create_task(self._run())

    async def shutdown(self) -> None:
        task, self._task = self._task, None
        if task is not None:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await task

    @property
    def running(self) -> bool:
        return self._task is not None and not self._task.done()

    async def _run(self) -> None:
        while True:
            try:
                await asyncio.sleep(self.poll)
                await self.tick(time.monotonic())
            except asyncio.CancelledError:
                raise
            except Exception:
                log.exception("session reminder tick failed")

    # Cflow-source compatibility surface ---------------------------------
    def scan(self, now: float):
        return self.cflow.scan(now)

    def skip(self, cwd: str, scope: str) -> bool:
        return self.cflow.skip(cwd, scope)

    def timers(self, now: Optional[float] = None):
        return self.cflow.timers(now)

    def session_paused(self, name: str) -> bool:
        """Whether a person paused repeating reminders for ``name``."""
        try:
            session = self.manager.get(name)
        except Exception:  # noqa: BLE001 - an unknown session has no control
            return False
        if getattr(session, "exited", False):
            return False
        reader = getattr(session, "reminders_paused", None)
        if callable(reader):
            return bool(reader())
        return bool(getattr(getattr(session, "sdef", None), "reminder_paused", False))

    def _rearm_session(self, name: str, now: Optional[float] = None) -> List[str]:
        """Re-arm every repeating source currently held for one session."""
        stamp = time.monotonic() if now is None else now
        sources: List[str] = []
        for (cwd, scope), entry in list(self._seen.items()):
            if scope != name or self.cflow._session_for(cwd, scope) is None:
                continue
            entry["at"] = stamp
            entry["held_at"] = None
            if "cflow" not in sources:
                sources.append("cflow")
        role = self._roles.get(name)
        if role is not None:
            role["at"] = stamp
            role["held_at"] = None
            sources.append("role")
        goal = self._goals.get(name)
        if goal is not None:
            goal.update(at=stamp, held_at=None)
            sources.append("score_goal")
        return sources

    def set_paused(
        self, name: str, paused: bool, *, now: Optional[float] = None
    ) -> bool:
        """Persist a session-level pause and start both clocks from now."""
        session = self.manager.get(name)
        if getattr(session, "exited", False):
            raise ValueError(f"session {name!r} has exited")
        setter = getattr(session, "set_reminder_pause", None)
        if not callable(setter):
            raise ValueError(f"session {name!r} cannot store a reminder pause")
        value = bool(setter(paused))
        # Pausing must not accumulate an immediate delivery debt, and resuming
        # must not release one.  Both transitions begin a fresh interval.
        self._rearm_session(name, now)
        persist = getattr(self.manager, "persist", None)
        if callable(persist):
            persist()
        return value

    def skip_session(
        self, name: str, *, now: Optional[float] = None
    ) -> List[str]:
        """Skip the next repeating delivery by re-arming active sources."""
        try:
            session = self.manager.get(name)
        except Exception:  # noqa: BLE001 - reported as no sources by the API
            return []
        if getattr(session, "exited", False) or self.session_paused(name):
            return []
        sources = self._rearm_session(name, now)
        if sources:
            log.info(
                "session reminder skipped once for %r (%s)",
                name,
                ", ".join(sources),
            )
        return sources

    def _measure(self, cwd: str, scope: str, awaits: dict, entry: dict, now: float):
        # ``scope`` is forwarded, not defaulted, and this wrapper is the reason
        # to say so. A probe subprocess resolves "which checkout am I" from
        # ``CLAUNCH_SESSION``, and in the daemon that variable names whichever
        # session started the daemon -- so the run's own scope has to travel
        # from the registry all the way to ``Popen(env=...)`` or the probe
        # measures a tree it was never about (:func:`cflow.engine.probe_env`,
        # issue ``claunch-04ru``). A compatibility shim that quietly dropped it
        # would put that defect back at exactly the layer nobody re-reads.
        return self.cflow._measure(cwd, scope, awaits, entry, now)

    def _session_for(self, cwd: str, scope: str):
        return self.cflow._session_for(cwd, scope)

    # Role source ---------------------------------------------------------
    def scan_roles(self, now: float, cfg: dict) -> List[Tuple[str, List[dict]]]:
        """Arm and return due roles without reading cflow state."""
        enabled, interval = role_reminder_policy(cfg)
        if not enabled:
            self._roles.clear()
            return []
        try:
            sessions = list(self.manager.list())
        except Exception:  # noqa: BLE001
            return []
        due: List[Tuple[str, List[dict]]] = []
        live = set()
        for session in sessions:
            if getattr(session, "exited", False):
                continue
            name = session.sdef.name
            entries = role_entries(name, self.manager, self.mesh)
            if not entries:
                self._roles.pop(name, None)
                continue
            live.add(name)
            key = role_key(entries)
            entry = self._roles.get(name)
            if entry is None:
                activity = self._session_activity(session)
                self._roles[name] = {
                    "key": key,
                    "at": now,
                    "fired_at": None,
                    "held_at": None,
                    "activity": activity,
                    # Nothing of ours is landing on a session we have not
                    # spoken to yet, so this reading needs no settling.
                    "settled": True,
                }
                continue
            if entry["key"] != key:
                # A role or stance update is current state the session has not
                # received.  It is due now, while first sight merely arms: the
                # initial opening already carried that first stance.
                entry.update({"key": key, "at": now - interval, "held_at": None})
            # The last reminder's own repaint and the turn it provoked land
            # after ``deliver`` returned, so the baseline recorded then is one
            # repaint stale.  Move it onto the screen that delivery actually
            # left behind before anything is compared against it.
            due_now = now - entry["at"] >= interval
            cflow_clock.settle_activity(session, entry, now, due=due_now)
            if due_now:
                # A role reminder is useful after the session has made
                # progress, but repeating it while the terminal has stayed
                # at the same meaningful screen only grows the pending
                # delivery queue.  Re-arm the timer when there is evidence
                # that nothing moved since the last successful reminder.
                # ``None`` means this session does not expose the activity
                # API (older/fake session implementations), so retain the
                # compatibility behaviour in that case.
                activity = self._session_activity(session)
                fired_at = entry.get("fired_at")
                unmoved = (
                    fired_at is not None
                    and activity is not None
                    and activity == entry.get("activity")
                )
                # The marker taken at delivery time has a blind spot both of
                # these cover: a reminder is typed into the terminal,
                # rendered there and answered, so that marker moves on every
                # delivery whether or not the session did any work.
                if not unmoved:
                    unmoved = cflow_clock.nothing_moved_since_settle(
                        session, entry
                    ) or cflow_clock.answered_only_the_reminder(
                        session, now, fired_at
                    )
                if unmoved:
                    entry["at"] = now
                    entry["held_at"] = None
                    continue
                due.append((name, entries))
        for name in list(self._roles):
            if name not in live:
                del self._roles[name]
        return due

    @staticmethod
    def _session_activity(session) -> Optional[str]:
        """Return the session's meaningful-screen activity marker.

        The marker is intentionally optional.  ``Session`` exposes it, while
        compatibility session objects used by older callers may not.
        """
        reader = getattr(session, "last_activity_at", None)
        if not callable(reader):
            return None
        try:
            return reader()
        except Exception:  # noqa: BLE001 - activity is decoration only
            return None

    def role_timers(self, now: Optional[float] = None) -> Dict[str, dict]:
        """In-memory role-source timers as ages, for diagnostics and tests."""
        at = time.monotonic() if now is None else now

        def ago(stamp):
            return None if stamp is None else max(0.0, at - stamp)

        return {
            name: {
                "armed_ago": ago(entry.get("at")),
                "fired_ago": ago(entry.get("fired_at")),
                "held_ago": ago(entry.get("held_at")),
                "key": entry.get("key"),
            }
            for name, entry in list(self._roles.items())
        }

    def status(
        self, name: str, *, now: Optional[float] = None, cfg: Optional[dict] = None,
        session=None,
    ) -> dict:
        """Header-facing state for the session-owned part of this service."""
        at = time.monotonic() if now is None else now
        if session is None:
            try:
                session = self.manager.get(name)
            except Exception:  # noqa: BLE001 - an absent session has no source
                return {"paused": False, "role": None}
        if getattr(session, "exited", False):
            return {"paused": False, "role": None}

        reader = getattr(session, "reminders_paused", None)
        paused = bool(reader()) if callable(reader) else bool(
            getattr(getattr(session, "sdef", None), "reminder_paused", False)
        )
        roles = role_entries(name, self.manager, self.mesh, session=session)
        if not roles:
            return {"paused": paused, "role": None}
        if cfg is None:
            cfg = self._config()
        enabled, interval = role_reminder_policy(cfg or {})
        timer = self._roles.get(name)
        view = {
            "running": self.running,
            "enabled": enabled,
            "interval": interval,
            "due_in": None,
            "fired_ago": None,
            "state": "",
        }
        if timer is not None:
            fired = timer.get("fired_at")
            view["fired_ago"] = None if fired is None else max(0.0, at - fired)

        if not self.running:
            view["state"] = "stopped"
        elif not enabled:
            view["state"] = "off"
        elif paused:
            view["state"] = "paused"
        elif timer is None:
            view["state"] = "arming"
        else:
            due = interval - max(0.0, at - timer["at"])
            view["due_in"] = due
            if due > 0:
                view["state"] = "counting"
            else:
                try:
                    busy = session.status() == STATUS_BUSY
                except Exception:  # noqa: BLE001 - raced with exit
                    busy = False
                view["state"] = "due" if busy else "held"
        return {"paused": paused, "role": view}

    def scan_goals(self, now: float, cfg: dict) -> set:
        """An opted-in goal also repeats without a role or cflow run."""
        interval = max(
            cflow_engine.REMINDER_MIN_INTERVAL,
            float(cfg.get("cflow_reminder_interval") or 600),
        )
        due, live = set(), set()
        for session in self.manager.list():
            if getattr(session, "exited", False) or not score_goal.active(session.sdef):
                continue
            name = session.sdef.name
            live.add(name)
            entry = self._goals.setdefault(
                name, {"at": now, "held_at": None, "fired_at": None}
            )
            if now - entry["at"] >= interval:
                # The same no-progress rule the other two sources apply: a
                # goal restated into a terminal that has done nothing since
                # the last one only grows the pending queue.
                if cflow_clock.answered_only_the_reminder(
                    session, now, entry.get("fired_at")
                ):
                    entry["at"] = now
                    entry["held_at"] = None
                    continue
                due.add(name)
        for name in set(self._goals) - live:
            del self._goals[name]
        return due

    async def tick(self, now: float) -> None:
        """One coordinated pass, grouping due sources by session."""
        cflow_due, cfg = await asyncio.gather(
            asyncio.to_thread(self.cflow.scan, now),
            asyncio.to_thread(self._config),
        )
        roles_due = (
            {name: entries for name, entries in self.scan_roles(now, cfg)}
            if cfg is not None
            else {}
        )
        goals_due = self.scan_goals(now, cfg) if cfg is not None else set()

        reminders: Dict[str, tuple] = {}
        for cwd, scope, block, kind in cflow_due:
            if kind == "signal":
                await self._deliver(cwd, scope, block, kind)
            elif not self.session_paused(scope):
                reminders[scope] = (cwd, block)

        for name in sorted(set(reminders) | set(roles_due) | goals_due):
            if self.session_paused(name):
                continue
            cflow_item = reminders.get(name)
            if cflow_item is not None:
                cwd, block = cflow_item
                await self._deliver_session(
                    name,
                    cwd=cwd,
                    cflow_block=block,
                    role_due=name in roles_due,
                    now=now,
                )
            else:
                await self._deliver_session(
                    name,
                    roles=roles_due.get(name),
                    role_due=name in roles_due,
                    now=now,
                )

    @staticmethod
    def _config() -> Optional[dict]:
        try:
            return store.daemon_config()
        except store.StoreError as exc:
            log.warning(
                "session reminder: config unreadable, role source skipped: %s", exc
            )
            return None

    async def _deliver(
        self, cwd: str, scope: str, block: str, kind: str = "reminder"
    ) -> None:
        """Compatibility delivery for one cflow-source result."""
        if kind == "signal":
            session = self.cflow._session_for(cwd, scope)
            if session is None:
                return
            try:
                delivered = await session.deliver(block)
            except Exception:
                log.exception("cflow signal delivery to %r failed", scope)
                return
            if delivered:
                self._mark_cflow(cwd, scope, kind)
            return
        await self._deliver_session(scope, cwd=cwd, cflow_block=block)

    async def _deliver_session(
        self,
        name: str,
        *,
        cwd: str = "",
        cflow_block: str = "",
        roles: Optional[List[dict]] = None,
        role_due: bool = False,
        now: Optional[float] = None,
    ) -> None:
        # One clock for the whole decision.  The scan that found this source
        # due and the stamps written back when it lands have to be on the
        # same reading, or "how long since I last spoke here" is measured
        # against a different origin than "how long has this terminal been
        # still" -- which is the comparison the no-progress rule makes.
        at = time.monotonic() if now is None else now
        if cwd:
            session = self.cflow._session_for(cwd, name)
            if session is None:
                return
        else:
            try:
                session = self.manager.get(name)
            except Exception:
                return
            if getattr(session, "exited", False):
                return

        cflow_entry = self._seen.get((cwd, name)) if cwd else None
        goal_entry = self._goals.get(name)
        cflow_full = bool(cflow_block) and not bool((cflow_entry or {}).get("restated"))
        current_roles = (
            roles if roles is not None else role_entries(name, self.manager, self.mesh)
        )

        if session.status() != STATUS_BUSY:
            stamp = at
            if cflow_entry is not None:
                cflow_entry["held_at"] = stamp
            if role_due and name in self._roles:
                self._roles[name]["held_at"] = stamp
            if goal_entry is not None:
                goal_entry["held_at"] = stamp
            log.debug("session reminder held for %r: session is not working", name)
            return

        # A reminder that became due while the session was idle is not useful
        # when the session starts a new turn.  The input that makes the
        # session busy is exactly the activity that makes the held reminder
        # obsolete, so begin a fresh interval instead of delivering it
        # immediately.  Keep this transition scoped to sources that were
        # actually held; an unrelated source may still be delivered normally.
        resumed = False
        if goal_entry is not None and goal_entry.get("held_at") is not None:
            goal_entry.update(at=at, held_at=None)
            resumed = True
        if cflow_entry is not None and cflow_entry.get("held_at") is not None:
            cflow_entry["at"] = at
            cflow_entry["held_at"] = None
            resumed = True
        role_entry = self._roles.get(name) if role_due else None
        if role_entry is not None and role_entry.get("held_at") is not None:
            role_entry["at"] = at
            role_entry["held_at"] = None
            resumed = True
        if resumed:
            log.debug(
                "session reminder re-armed for %r after idle session resumed", name
            )
            return

        try:
            open_asks = len(await asyncio.to_thread(cflow_engine.open_asks, name))
        except Exception:  # noqa: BLE001 - decoration never sinks a delivery
            open_asks = 0
        situation = situation_lines(name, self.manager, self.mesh, open_asks)
        context = (
            context_id_lines(name, self.manager, self.mesh)
            if (cflow_full or role_due)
            else []
        )
        def render():
            active = score_goal.active(session.sdef)
            if not cflow_block and not role_due and not active:
                return ""
            return reminder_block(
                name,
                goal=score_goal.prompt(session.sdef.user_score) if active else "",
                roles=current_roles,
                cflow=cflow_block,
                situation=situation,
                context=context,
                cflow_guidance=cflow_full,
            )

        # Score may change while delivery waits for the person's input draft.
        block = render if getattr(session.sdef, "score_goal", False) else render()
        if not block:
            return
        try:
            delivered = await session.deliver(block)
        except Exception:
            log.exception("session reminder delivery to %r failed", name)
            return
        if not delivered:
            return
        if goal_entry is not None:
            goal_entry.update(at=at, fired_at=at, held_at=None)
        if cflow_block:
            self._mark_cflow(cwd, name, "reminder", now=at)
        if current_roles:
            self._mark_role(name, now=at)
        log.info(
            "session reminder delivered to %r (role=%s, cflow=%s)",
            name,
            bool(current_roles),
            bool(cflow_block),
        )

    def _mark_cflow(
        self, cwd: str, scope: str, kind: str, *, now: Optional[float] = None
    ) -> None:
        entry = self._seen.get((cwd, scope))
        if entry is None:
            return
        stamp = time.monotonic() if now is None else now
        entry["at"] = stamp
        entry["fired_at"] = stamp
        entry["fired_kind"] = kind
        entry["held_at"] = None
        if kind == "reminder":
            entry["restated"] = True
        # Anchor the no-progress suppression baseline to the screen state the
        # delivery actually landed on, so a later scan compares like with like.
        # Provisional: what the delivery is about to do to this terminal has
        # not happened yet, so the scan re-takes it once it has (see
        # :func:`cflow_clock.settle_activity`).
        session = self.cflow._session_for(cwd, scope)
        if session is not None:
            entry["activity"] = self._session_activity(session)
        entry["settled"] = False

    def _mark_role(self, name: str, *, now: Optional[float] = None) -> None:
        entry = self._roles.get(name)
        if entry is None:
            return
        stamp = time.monotonic() if now is None else now
        entry["at"] = stamp
        entry["fired_at"] = stamp
        try:
            session = self.manager.get(name)
        except Exception:  # noqa: BLE001 - the session may exit after send
            session = None
        if session is not None:
            entry["activity"] = self._session_activity(session)
        # ...and that reading is provisional. The submit repaint and the turn
        # this delivery provokes both land after it, so a later scan re-takes
        # the baseline once they have (:func:`cflow_clock.settle_activity`).
        entry["settled"] = False
        entry["held_at"] = None
