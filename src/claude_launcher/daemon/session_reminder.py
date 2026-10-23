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
from dataclasses import replace
from typing import Dict, List, Optional, Tuple

from pathlib import Path

from .. import digests, prompt_packs, store, workspaces
from ..cflow import engine as cflow_engine, state as cflow_state
from . import cflow_clock, daemon_prompt, mesh_roles, rebrief, score_goal, session_packs, wallclock
from .session import STATUS_BUSY, delivery_wake

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


def pack_reminder_policy(cfg: dict) -> Tuple[bool, float]:
    """Effective machine policy for the prompt pack source.

    Its own switch and interval, on the same lower bound as the role source:
    a session's packs are standing rules recorded at creation, independent of
    whether it holds a role or drives a run.
    """
    enabled = bool(cfg.get("prompt_pack_reminder", True))
    interval = float(cfg.get("prompt_pack_reminder_interval") or 0)
    if interval > 0:
        interval = max(interval, cflow_engine.REMINDER_MIN_INTERVAL)
    return enabled and interval > 0, interval


def pack_entries(sdef) -> List[dict]:
    """The prompt packs one session holds, as the reminder names them.

    Read from the definition's records, the same text ``rebrief`` serves
    back, so an id named here is always one that tool can answer. After them
    come the packs a person sent from the packs pane and that are held for
    recovery (:func:`session_packs.held_entries`, source ``sent``) --
    ``rebrief`` answers for those too.
    """
    out: List[dict] = []
    for rec in getattr(sdef, "prompt_packs", ()) or ():
        text = (rec.get("text") or "").strip()
        if not rec.get("id") or not text:
            continue
        out.append(
            {
                "id": str(rec["id"]),
                "digest": prompt_packs.text_id(text),
                "source": rec.get("source") or prompt_packs.WORKSPACE,
                "required_by": list(rec.get("required_by") or ()),
            }
        )
    name = getattr(sdef, "name", "")
    if name:
        try:
            out.extend(session_packs.held_entries(name))
        except Exception:  # noqa: BLE001 - decoration never sinks a reminder
            log.warning("held prompt packs unreadable for %r", name, exc_info=True)
    return out


def pack_key(entries: List[dict]) -> tuple:
    """The pack source's position key: which packs, with which text.

    The source is left out on purpose. A claunch copy that a workspace
    installed unchanged is a new origin for the same text, and the agent
    holds that text under the same id, so there is nothing to tell it.
    """
    return tuple((e["id"], e["digest"]) for e in entries)


def _session_workspace(sdef) -> Optional[Path]:
    """The registered workspace a session's directory belongs to, if any.

    The same lookup creation uses to choose the packs (``api.
    _session_prompt_packs``), so a recorded workspace copy is compared
    against the file it was read from. ``None`` means the workspace could
    not be confirmed -- an unregistered directory or a failed lookup alike
    -- and :func:`refresh_packs` then removes nothing.
    """
    try:
        owner = workspaces.owning(str(cflow_state.resolve_cwd(sdef.cwd or None)))
    except Exception:  # noqa: BLE001 - an unresolvable cwd has no workspace
        return None
    return Path(owner.path) if owner is not None else None


def _current_record(
    rec: dict, workspace: Optional[Path], memo: dict
) -> Optional[dict]:
    """The record ``rec`` should be now, or None when its copy is gone."""
    source = rec.get("source") or prompt_packs.WORKSPACE
    if source == "prompter":
        return rec  # Server snippets are the snapshot selected at creation.
    if source == prompt_packs.DAEMON:
        if "daemon" not in memo:
            memo["daemon"] = daemon_prompt.pack()
        pack = memo["daemon"]
        if pack is None:
            return None
        return {**rec, "text": pack.body}
    pack = prompt_packs.current_copy(workspace, rec, memo=memo)
    if pack is None:
        return None
    if pack.source == prompt_packs.WORKSPACE:
        # A workspace copy's record carries no origin keys
        # (prompt_packs.records): its file explains why it is there. The
        # one exception is ``always`` -- it was given because the workspace
        # gives it to every session, and its header keeps saying so.
        out = {"id": rec["id"], "text": pack.body}
        if rec.get("always"):
            out["always"] = True
        return out
    return {**rec, "text": pack.body, "source": pack.source}


def refresh_packs(
    session, memo: Optional[dict] = None
) -> Tuple[Optional[tuple], List[dict]]:
    """Bring one session's recorded packs up to the copies they follow.

    Returns ``(records, notices)``: the new records when anything moved
    (``None`` otherwise), and one notice per pack whose text changed or whose
    copy is gone. A change of origin with the same text updates the record
    and says nothing (:func:`pack_key`). The caller stores the records; this
    only reads files, each one once per ``memo`` (one scan shares it).
    """
    memo = {} if memo is None else memo
    sdef = session.sdef
    recorded = tuple(getattr(sdef, "prompt_packs", ()) or ())
    if not recorded:
        return None, []
    owner_key = ("owner", sdef.cwd or "")
    if owner_key not in memo:
        memo[owner_key] = _session_workspace(sdef)
    workspace = memo[owner_key]
    out: List[dict] = []
    notices: List[dict] = []
    moved = False
    for rec in recorded:
        source = rec.get("source") or prompt_packs.WORKSPACE
        if workspace is None and source == prompt_packs.WORKSPACE:
            # Unconfirmed is not gone: a workspace that was unregistered or
            # could not be read still has its file, and a removal is not
            # undone by registering it again.
            out.append(rec)
            continue
        try:
            now = _current_record(rec, workspace, memo)
        except Exception:  # noqa: BLE001 - an unreadable copy is not a removal
            out.append(rec)
            continue
        old_id = prompt_packs.text_id(rec.get("text") or "")
        if now is None and workspace is None and source == prompt_packs.PACKAGE:
            # claunch's copy is gone but the workspace, which might hold
            # this pack's replacement, cannot be looked at: keep it.
            out.append(rec)
            continue
        if now is None:
            moved = True
            notices.append({"id": rec["id"], "change": "removed", "old": old_id})
            continue
        new_id = prompt_packs.text_id(now.get("text") or "")
        if now != rec:
            moved = True
        if new_id != old_id:
            notices.append(
                {"id": rec["id"], "change": "changed", "old": old_id, "new": new_id}
            )
        out.append(now)
    return (tuple(out) if moved else None), notices


def pack_section(entries: List[dict], notices: List[dict]) -> List[str]:
    """The Prompt packs section: ids only, never the text."""
    lines: List[str] = []
    for note in notices:
        if note["change"] == "removed":
            lines.append(
                f"removed: prompt pack {note['id']} (text id {note['old']}) no "
                "longer applies to this session -- its copy is gone. Stop "
                "acting from it."
            )
        else:
            lines.append(
                f"changed: prompt pack {note['id']} was edited; its text id is "
                f"now {note['new']} (was {note['old']}). The text you hold "
                f"under {note['old']} is replaced: call the mesh 'rebrief' "
                f"tool with id {note['new']} and act from what it returns."
            )
    for entry in entries:
        why = ""
        if entry.get("required_by"):
            why = f", required by role {', '.join(entry['required_by'])}"
        lines.append(
            f"prompt pack {entry['id']}: text id {entry['digest']} "
            f"({entry['source']}{why})"
        )
    if entries:
        lines.append(
            "recovery: find each id attached to its pack text in this "
            "conversation. If it is absent, call the mesh 'rebrief' tool with "
            "that id; this id line alone is not the pack."
        )
    return lines


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
                        # Held beside the primary: a lookup by role finds the
                        # member under these too, so the reminder says so --
                        # and a change to them is a change of position.
                        "subroles": list(getattr(member, "subroles", None) or ()),
                        "stance": stance,
                        "digest": digests.text_digest(stance),
                        # The subroles' stances bind as well and were handed
                        # beside their own ids (``MeshManager._stance_lines``).
                        "subrole_stances": [
                            {"name": sub, "digest": digests.text_digest(text)}
                            for sub, text in rebrief._subrole_stances(mesh_mgr, mesh, member)
                        ],
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
    return tuple(
        (e["mesh"], e["name"], e["digest"], tuple(e.get("subroles") or ()),
         tuple(s["digest"] for s in e.get("subrole_stances") or ()))
        for e in entries
    )


def role_section(entries: List[dict], *, cflow_guidance: bool) -> List[str]:
    """The Role section, with stance recovery and optional step guidance."""
    lines: List[str] = []
    for index, entry in enumerate(entries):
        if index:
            lines.append("")
        where = f" on {entry['mesh']}" if entry["mesh"] else ""
        lines.append(f"role: {entry['name']}{where}")
        if entry.get("subroles"):
            lines.append(
                f"subroles: {', '.join(entry['subroles'])} (you also answer "
                "for these when a workflow or a peer looks one up)"
            )
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
        for sub in entry.get("subrole_stances") or ():
            if sub.get("digest"):
                lines.append(
                    f"subrole stance text id ({sub['name']}): {sub['digest']} "
                    "— find it beside that stance text in this conversation, "
                    "or call the mesh 'rebrief' tool with that id"
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
    """IDs for stable session text no other section names: the opening task.

    Stances are the Role section's, and prompt packs have their own source
    and section (:func:`pack_section`).
    """
    try:
        ids = [
            (ident, kind)
            for ident, kind in rebrief.given_ids(
                name, manager=manager, mesh_mgr=mesh_mgr
            )
            if not kind.startswith(("stance (", "prompt pack ("))
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
    packs: Optional[List[str]] = None,
    instructions: str = "",
) -> str:
    """Render one session reminder with its peer source sections.

    ``instructions`` is the person's temporary instructions for this session
    (:attr:`SessionDef.reminder_note`). It is no source of its own -- it
    never makes a reminder due -- and rides every delivery as the last
    section, after everything the daemon itself states.
    """
    sections: List[Tuple[str, List[str]]] = []
    if goal:
        sections.append(("Score goal", [goal]))
    role_lines = role_section(roles, cflow_guidance=cflow_guidance)
    if role_lines:
        sections.append(("Role", role_lines))
    pack_lines = list(packs or [])
    if pack_lines:
        sections.append(("Prompt packs", pack_lines))
    cflow_lines = _cflow_section(cflow)
    if cflow_lines:
        sections.append(("Cflow", cflow_lines))
    context_lines = list(context or [])
    if context_lines:
        sections.append(("Context", context_lines))
    situation_lines = _situation_section(list(situation or []))
    if situation_lines:
        sections.append(("Situation", situation_lines))
    note = str(instructions or "").strip()
    if note:
        sections.append(
            (
                "Temporary instructions",
                [
                    "set by a person for this session only, and restated "
                    "with every reminder until they clear it -- follow them "
                    "alongside your other instructions:",
                    *note.splitlines(),
                ],
            )
        )

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
        self._packs: Dict[str, dict] = {}
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
        packs = self._packs.get(name)
        if packs is not None:
            packs["at"] = stamp
            packs["held_at"] = None
            sources.append("prompt_packs")
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

    # Prompt pack source --------------------------------------------------
    def _store_packs(self, session, records: tuple) -> None:
        """Write a session's refreshed pack records and persist them."""
        session.sdef = replace(session.sdef, prompt_packs=records)
        persist = getattr(self.manager, "persist", None)
        if callable(persist):
            try:
                persist()
            except Exception:  # noqa: BLE001 - the next persist writes it
                log.warning("prompt pack refresh: persist failed", exc_info=True)

    def scan_packs(self, now: float, cfg: dict) -> List[Tuple[str, List[dict]]]:
        """Refresh recorded packs, arm, and return the sessions due.

        A changed or removed copy is applied to the session's record here,
        whether or not the source is enabled: ``rebrief`` answers from that
        record, and it must not go on serving a text the workspace replaced.
        What the session is told about it waits as a notice on its timer
        entry until a delivery lands.
        """
        enabled, interval = pack_reminder_policy(cfg)
        try:
            sessions = list(self.manager.list())
        except Exception:  # noqa: BLE001
            return []
        due: List[Tuple[str, List[dict]]] = []
        live = set()
        # Sessions in one workspace share its directory and claunch's copies:
        # read each once per scan, since this runs on every poll.
        memo: dict = {}
        for session in sessions:
            if getattr(session, "exited", False):
                continue
            name = session.sdef.name
            try:
                records, notices = refresh_packs(session, memo)
            except Exception:  # noqa: BLE001 - one session's files, not all
                log.warning("prompt pack refresh failed for %r", name, exc_info=True)
                records, notices = None, []
            if records is not None:
                self._store_packs(session, records)
            entries = pack_entries(session.sdef)
            if not enabled:
                continue
            if not entries and not notices and name not in self._packs:
                continue
            live.add(name)
            key = pack_key(entries)
            entry = self._packs.get(name)
            if entry is None:
                entry = self._packs[name] = {
                    "key": key,
                    "at": now,
                    "fired_at": None,
                    "held_at": None,
                    "activity": self._session_activity(session),
                    # The opening carried these packs beside their ids.
                    "settled": True,
                    "moved_for": None,
                    "notices": [],
                }
            if notices:
                entry["notices"].extend(notices)
            if entry["key"] != key or notices:
                # Like a role change: current state the session has not
                # received is due now, and earns no repeat rule.
                entry.update(
                    {"key": key, "at": now - interval, "held_at": None,
                     "moved_for": None}
                )
            if not entries and not entry["notices"]:
                # Every pack is gone and the session has been told.
                del self._packs[name]
                live.discard(name)
                continue
            due_now = now - entry["at"] >= interval
            cflow_clock.settle_activity(session, entry, now, due=due_now)
            cflow_clock.observe_movement(session, entry, now)
            if due_now and not entry["notices"]:
                # The same two no-progress rules as the role source.
                if not cflow_clock.moved_enough(session, entry):
                    continue
                activity = self._session_activity(session)
                fired_at = entry.get("fired_at")
                unmoved = (
                    fired_at is not None
                    and activity is not None
                    and activity == entry.get("activity")
                )
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
            if due_now:
                due.append((name, entries))
        for name in list(self._packs):
            if name not in live:
                del self._packs[name]
        return due

    def _mark_packs(self, name: str, *, now: Optional[float] = None) -> None:
        entry = self._packs.get(name)
        if entry is None:
            return
        stamp = time.monotonic() if now is None else now
        entry["at"] = stamp
        entry["fired_at"] = stamp
        entry["notices"] = []
        try:
            session = self.manager.get(name)
        except Exception:  # noqa: BLE001 - the session may exit after send
            session = None
        if session is not None:
            entry["activity"] = self._session_activity(session)
        entry["settled"] = False
        entry["moved_for"] = None
        entry["held_at"] = None
        if not entry["key"]:
            # Nothing is left to remind of once the removal has been told.
            del self._packs[name]

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
                    # spoken to yet, so this reading needs no settling and
                    # its first reminder is not a repeat to be earned.
                    "settled": True,
                    "moved_for": None,
                }
                continue
            # A role or stance update is current state the session has not
            # received.  It is due now, while first sight merely arms: the
            # initial opening already carried that first stance.  The
            # movement total goes with it for the same reason the rules
            # below are skipped: this delivery carries something new.
            changed = entry["key"] != key
            if changed:
                entry.update(
                    {"key": key, "at": now - interval, "held_at": None,
                     "moved_for": None}
                )
            # The last reminder's own repaint and the turn it provoked land
            # after ``deliver`` returned, so the baseline recorded then is one
            # repaint stale.  Move it onto the screen that delivery actually
            # left behind before anything is compared against it.
            due_now = now - entry["at"] >= interval
            cflow_clock.settle_activity(session, entry, now, due=due_now)
            cflow_clock.observe_movement(session, entry, now)
            # Both rules below ask whether this terminal has earned the same
            # reminder a second time.  A changed key makes it a different
            # reminder, so neither applies: a session holding a stance that
            # has since been rewritten is not corrected by having worked.
            if due_now and not changed:
                if not cflow_clock.moved_enough(session, entry):
                    # Due, but this terminal has not been working. Left due
                    # rather than re-armed, so it speaks as soon as it is.
                    continue
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
            if due_now:
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
        # Absolute times, not ages: an age moves on every poll and made every
        # sessions answer different bytes (claunch-4thlk.1, daemon/wallclock).
        view = {
            "running": self.running,
            "enabled": enabled,
            "interval": interval,
            "due_at": None,
            "fired_at": None,
            "state": "",
        }
        if timer is not None:
            view["fired_at"] = wallclock.wall(timer.get("fired_at"))

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
            view["due_at"] = wallclock.wall(timer["at"] + interval)
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
                name,
                {
                    "at": now,
                    "held_at": None,
                    "fired_at": None,
                    "activity": None,
                    "output_at": None,
                    "settled": True,
                    "moved_for": None,
                },
            )
            # The same no-progress rule the other two sources carry, on the
            # same two readings: a goal restated into a terminal that has
            # done nothing since the last one only grows the pending queue.
            due_now = now - entry["at"] >= interval
            cflow_clock.settle_activity(session, entry, now, due=due_now)
            cflow_clock.observe_movement(session, entry, now)
            if due_now and not cflow_clock.moved_enough(session, entry):
                # Due, but this terminal has not been working. Left due
                # rather than re-armed, so it speaks as soon as it is.
                continue
            if due_now:
                if cflow_clock.nothing_moved_since_settle(
                    session, entry
                ) or cflow_clock.answered_only_the_reminder(
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
        packs_due = (
            {name: entries for name, entries in self.scan_packs(now, cfg)}
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

        for name in sorted(
            set(reminders) | set(roles_due) | set(packs_due) | goals_due
        ):
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
                    packs=packs_due.get(name),
                    packs_due=name in packs_due,
                    now=now,
                )
            else:
                await self._deliver_session(
                    name,
                    roles=roles_due.get(name),
                    role_due=name in roles_due,
                    packs=packs_due.get(name),
                    packs_due=name in packs_due,
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
                with delivery_wake("notice"):
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
        packs: Optional[List[dict]] = None,
        packs_due: bool = False,
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
            if packs_due and name in self._packs:
                self._packs[name]["held_at"] = stamp
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
        pack_entry = self._packs.get(name) if packs_due else None
        if (
            pack_entry is not None
            and pack_entry.get("held_at") is not None
            and not pack_entry.get("notices")
        ):
            # A held change notice does not go stale when the session
            # resumes: it is news, so it rides the next turn instead.
            pack_entry["at"] = at
            pack_entry["held_at"] = None
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
        pack_lines = (
            pack_section(
                packs if packs is not None else pack_entries(session.sdef),
                list((self._packs.get(name) or {}).get("notices") or ()),
            )
            if packs_due
            else []
        )

        def render():
            active = score_goal.active(session.sdef)
            if not cflow_block and not role_due and not pack_lines and not active:
                return ""
            return reminder_block(
                name,
                goal=score_goal.prompt(session.sdef) if active else "",
                roles=current_roles,
                cflow=cflow_block,
                situation=situation,
                context=context,
                cflow_guidance=cflow_full,
                packs=pack_lines,
                instructions=getattr(session.sdef, "reminder_note", None) or "",
            )

        # Score may change while delivery waits for the person's input draft.
        block = render if getattr(session.sdef, "score_goal", False) else render()
        if not block:
            return
        try:
            with delivery_wake("notice"):
                delivered = await session.deliver(block)
        except Exception:
            log.exception("session reminder delivery to %r failed", name)
            return
        if not delivered:
            return
        if goal_entry is not None:
            goal_entry.update(
                at=at, fired_at=at, held_at=None,
                # Provisional, for the same reason the other two sources
                # mark theirs so: the delivery's own effects land later.
                activity=self._session_activity(session), settled=False,
                moved_for=None,
            )
        if cflow_block:
            self._mark_cflow(cwd, name, "reminder", now=at)
        if current_roles:
            self._mark_role(name, now=at)
        if packs_due:
            self._mark_packs(name, now=at)
        log.info(
            "session reminder delivered to %r (role=%s, cflow=%s, packs=%s)",
            name,
            bool(current_roles),
            bool(cflow_block),
            bool(packs_due),
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
        entry["moved_for"] = None

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
        entry["moved_for"] = None
        entry["held_at"] = None
