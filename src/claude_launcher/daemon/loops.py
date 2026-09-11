"""Open loops: what a session is waiting on, kept where a reset cannot lose it.

A re-briefing (:mod:`rebrief`) restores everything the daemon can re-derive
— roster, run position, children, the opening task. What it could not
restore was the half that only ever lived in the agent's context: *I am
waiting for s501's inbox to drain so I can re-send the assignment*, *I asked
w2 a question and its answer changes what I merge*. After a compaction that
knowledge is gone, the agent resumes from a summary that may or may not have
kept it, and the thing it was waiting on is simply never picked up again.
Observed on mesh-0826, 2026-09-10: a leader's re-send of two board
assignments, held back by a full inbox, survived only as a sentence in its
own conversation summary.

This module is the ledger for that half. One JSON file per session
(``<session dir>/loops.json``), each entry saying what is waited on, since
when, what would resume it, and what to do then. Three writers:

* the agent, through the ``loop_add`` / ``loop_close`` MCP tools and
  ``claunch loops``;
* the mesh, on the two events it can see and the agent cannot re-derive: a
  send refused for a full inbox (:meth:`MeshManager._send_core` — the
  message *does not exist* afterwards, so the intent to re-send has no other
  home), closed again by the next send that reaches that member;
* nobody, for reply-waits: those are read straight off the mesh's response
  watches (:func:`reply_waits`) — a delivered ask already has a record that
  a threaded reply clears, and a second copy of it here would only drift.

Entries expire. An open loop nobody closed is not proof the wait is still
real, so past ``expires_at`` a re-briefing marks it *stale* rather than
serving it as current — the agent decides whether to close or renew it.
Nothing here is deleted automatically: a closed entry stays on file, so a
ledger read after the fact still shows what was waited on and when it
resolved.
"""

from __future__ import annotations

import json
import logging
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional

from . import paths

log = logging.getLogger(__name__)

#: A wait with no stated horizon is presumed real for this long. Six hours
#: covers a working shift; a loop older than that was either forgotten or
#: is worth restating with a horizon of its own.
DEFAULT_TTL_SECS = 6 * 3600

#: Lines a re-briefing spends on this ledger. Past the cap it says how many
#: more there are and points at the tool — the block has a hard budget and
#: the opening task must still fit after it.
REBRIEF_LIMIT = 10

#: How much of a ``what`` a one-line rendering keeps.
_LINE_WHAT = 120

_FIELDS = (
    "id", "kind", "key", "what", "since", "resume_when", "then", "refs",
    "expires_at", "closed_at", "closed_note",
)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


def _parse(ts: Optional[str]) -> Optional[datetime]:
    if not ts:
        return None
    try:
        dt = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def ledger_file(session: str) -> Path:
    return paths.session_dir(session) / "loops.json"


def _load(session: str) -> List[dict]:
    path = ledger_file(session)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return []
    except (OSError, ValueError) as exc:
        log.warning("loops: cannot read %s: %s", path, exc)
        return []
    rows = doc.get("loops") if isinstance(doc, dict) else None
    return [r for r in rows if isinstance(r, dict)] if isinstance(rows, list) else []


def _save(session: str, rows: List[dict]) -> None:
    path = ledger_file(session)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(".json.tmp")
    tmp.write_text(
        json.dumps({"loops": rows}, ensure_ascii=False, indent=1), encoding="utf-8"
    )
    tmp.replace(path)


def is_open(entry: dict) -> bool:
    return not entry.get("closed_at")


def is_stale(entry: dict, now: Optional[datetime] = None) -> bool:
    """Open, and past the horizon it was given."""
    if not is_open(entry):
        return False
    exp = _parse(entry.get("expires_at"))
    return exp is not None and exp <= (now or utcnow())


def add(
    session: str,
    what: str,
    *,
    resume_when: str = "",
    then: str = "",
    refs: Optional[dict] = None,
    kind: str = "manual",
    key: Optional[str] = None,
    expires_in: Optional[float] = None,
) -> dict:
    """Record one open loop for ``session`` and return it.

    ``key`` makes an entry idempotent: a second ``add`` with the same key
    while the first is still open updates that entry in place (fresh
    ``what``, fresh horizon) instead of stacking a duplicate — the mesh
    writes one per (mesh, recipient) and a sender refused three times has
    ONE re-send to do, not three. ``expires_in`` is seconds from now;
    omitted, :data:`DEFAULT_TTL_SECS` applies.
    """
    what = " ".join(str(what or "").split())
    if not what:
        raise ValueError("'what' is required")
    now = utcnow()
    ttl = float(expires_in) if expires_in is not None else DEFAULT_TTL_SECS
    if ttl <= 0:
        raise ValueError("'expires_in' must be a positive number of seconds")
    rows = _load(session)
    entry: Optional[dict] = None
    if key:
        entry = next(
            (r for r in rows if r.get("key") == key and is_open(r)), None
        )
    if entry is None:
        entry = {
            "id": "loop-" + uuid.uuid4().hex[:8],
            "kind": str(kind or "manual"),
            "key": key or None,
            "since": _iso(now),
            "closed_at": None,
            "closed_note": "",
        }
        rows.append(entry)
    entry.update(
        {
            "what": what,
            "resume_when": " ".join(str(resume_when or "").split()),
            "then": " ".join(str(then or "").split()),
            "refs": dict(refs) if isinstance(refs, dict) else {},
            "expires_at": _iso(now + timedelta(seconds=ttl)),
        }
    )
    _save(session, rows)
    return dict(entry)


def close(session: str, loop_id: str, note: str = "") -> Optional[dict]:
    """Close one entry by id. ``None`` when there is no such open entry."""
    rows = _load(session)
    for entry in rows:
        if entry.get("id") == loop_id and is_open(entry):
            entry["closed_at"] = _iso(utcnow())
            entry["closed_note"] = " ".join(str(note or "").split())
            _save(session, rows)
            return dict(entry)
    return None


def close_where(
    session: str, predicate: Callable[[dict], bool], note: str = ""
) -> List[dict]:
    """Close every open entry ``predicate`` accepts; returns what was closed."""
    rows = _load(session)
    closed: List[dict] = []
    stamp = _iso(utcnow())
    for entry in rows:
        if is_open(entry) and predicate(entry):
            entry["closed_at"] = stamp
            entry["closed_note"] = " ".join(str(note or "").split())
            closed.append(dict(entry))
    if closed:
        _save(session, rows)
    return closed


def close_key(session: str, key: str, note: str = "") -> List[dict]:
    return close_where(session, lambda e: e.get("key") == key, note)


def open_entries(session: str) -> List[dict]:
    """This session's open loops, oldest first, each flagged ``stale``."""
    now = utcnow()
    rows = [dict(r) for r in _load(session) if is_open(r)]
    for r in rows:
        r["stale"] = is_stale(r, now)
    rows.sort(key=lambda r: str(r.get("since") or ""))
    return rows


def all_entries(session: str) -> List[dict]:
    now = utcnow()
    rows = [dict(r) for r in _load(session)]
    for r in rows:
        r["stale"] = is_stale(r, now)
    rows.sort(key=lambda r: str(r.get("since") or ""))
    return rows


# --------------------------------------------------------------------------- #
# the mesh's two automatic writers
# --------------------------------------------------------------------------- #
def resend_key(mesh: str, handle: str) -> str:
    return f"resend:{mesh}:{handle}"


def note_refused_send(
    session: str, mesh: str, deferred: List[dict], body: str
) -> None:
    """A send bounced for a full inbox: the intent to re-send is the loop.

    One entry per refused recipient, keyed so repeats fold into it. Never
    raises — the send's own outcome is what the caller must report, and a
    ledger that cannot be written is a warning in the daemon log.
    """
    head = " ".join(str(body or "").split())
    if len(head) > _LINE_WHAT:
        head = head[:_LINE_WHAT] + "..."
    for entry in deferred:
        handle = str(entry.get("handle") or "")
        if not handle:
            continue
        try:
            add(
                session,
                f"re-send to {handle} in mesh {mesh}: {head}",
                kind="resend",
                key=resend_key(mesh, handle),
                resume_when=(
                    f"{handle}'s inbox drains ({entry.get('queued')} waiting, "
                    f"cap {entry.get('inbox_max')}) -- the mesh refused this "
                    "and queued nothing"
                ),
                then=f"claunch mesh send {mesh} {handle} \"...\" again",
                refs={"mesh": mesh, "handle": handle},
            )
        except Exception as exc:  # noqa: BLE001 — never fail the send for this
            log.warning("loops: cannot record refused send for %r: %s", session, exc)


def note_delivered_send(session: str, mesh: str, recipients: List[str]) -> None:
    """A send reached these members: any re-send loop toward them is done."""
    for handle in recipients:
        try:
            close_key(
                session, resend_key(mesh, handle),
                note=f"a later send to {handle} was accepted",
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("loops: cannot close resend loop for %r: %s", session, exc)


def reply_waits(session: str, mesh_mgr) -> List[dict]:
    """Replies this session is owed, read off the mesh's response watches.

    Derived, not stored: a watch is opened when a reply-expecting message of
    this session's is DELIVERED and cleared by a threaded reply
    (:meth:`MeshManager._settle_response_watch`), which is exactly the
    lifetime a reply-wait loop would have — so the watch IS the loop, and
    the ledger keeps no second copy to fall out of step with it.
    """
    out: List[dict] = []
    if mesh_mgr is None:
        return out
    try:
        rows = mesh_mgr.meshes_for_session(session)
    except Exception:  # noqa: BLE001
        return out
    for row in rows:
        try:
            mesh = mesh_mgr.get(row["mesh"])
            member = mesh_mgr.member_for_session(mesh, session)
        except Exception:  # noqa: BLE001
            continue
        if member is None:
            continue
        for watch in mesh.response_watches.values():
            if str(watch.get("from") or "") != member.handle:
                continue
            out.append(
                {
                    "id": f"reply:{watch.get('id')}",
                    "kind": "reply",
                    "what": (
                        f"reply from {watch.get('to')} to your message "
                        f"{watch.get('id')} in mesh {mesh.name}"
                    ),
                    "since": str(watch.get("delivered_at") or ""),
                    "resume_when": f"{watch.get('to')} answers with --reply-to",
                    "then": "act on the answer",
                    "refs": {
                        "mesh": mesh.name, "message": watch.get("id"),
                        "handle": watch.get("to"),
                    },
                    "stale": False,
                    "derived": True,
                }
            )
    out.sort(key=lambda r: str(r.get("since") or ""))
    return out


# --------------------------------------------------------------------------- #
# rendering
# --------------------------------------------------------------------------- #
def line(entry: dict) -> str:
    what = str(entry.get("what") or "")
    if len(what) > _LINE_WHAT:
        what = what[:_LINE_WHAT] + "..."
    tag = "STALE " if entry.get("stale") else ""
    since = str(entry.get("since") or "?")
    text = f"- {tag}[{entry.get('id')}] {what} (since {since})"
    if entry.get("resume_when"):
        text += f"; resumes when: {entry['resume_when']}"
    if entry.get("then"):
        text += f"; then: {entry['then']}"
    return text


def rebrief_section(session: str, mesh_mgr=None) -> str:
    """The block a re-briefing carries, or ``""`` when nothing is open."""
    rows = open_entries(session) + reply_waits(session, mesh_mgr)
    if not rows:
        return ""
    shown = rows[:REBRIEF_LIMIT]
    stale = sum(1 for r in rows if r.get("stale"))
    lines = [
        "---",
        "# claunch loops: what you were waiting on -- machine-generated",
        f"open: {len(rows)}" + (f" ({stale} stale)" if stale else ""),
    ]
    lines.extend(line(r) for r in shown)
    if len(rows) > len(shown):
        lines.append(
            f"[... {len(rows) - len(shown)} more -- the 'loops' tool lists them]"
        )
    lines.append(
        "note: a STALE loop passed its horizon unclosed -- decide whether it "
        "is still real (loop_add renews it) or done (loop_close). Close a loop "
        "when its wait ends; a reply: entry closes itself when the answer "
        "arrives threaded."
    )
    lines.append("---")
    return "\n".join(lines)


def summary(session: str, mesh_mgr=None) -> Dict[str, object]:
    stored = open_entries(session)
    derived = reply_waits(session, mesh_mgr)
    return {
        "session": session,
        "open": stored + derived,
        "stale": sum(1 for r in stored if r.get("stale")),
    }
