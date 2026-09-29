"""Mesh: session-to-session messaging delivered by typing into PTYs.

Sessions are grouped into a *mesh*; each member gets a handle, and messages
sent between members are **injected into the recipient's terminal** by the
daemon (bracketed paste + Enter). Arrival is the wake-up — receivers need no
watcher, no polling and no hooks, which is why the whole doorbell/nudge
apparatus of file-based agent meshes is absent here by design (see
``docs/mesh-design.md``).

Ownership model (federation v2): every mesh has ONE authoritative **primary**
daemon — its creator. The primary owns the member registry, THE message log
(one sequence), the policy engine and invite minting. Other daemons join by
redeeming an invite and hold a **mirror**: a synced copy of roster + log for
reading, terminal delivery for their own local member sessions, and a durable
outbox toward the primary. All member operations from a guest daemon (join /
leave / send — even a DM between two members of the same guest) are requests
forwarded to the primary, which decides, sequences and fans out. Topology is
hub-and-spoke: guests talk only to the primary, so guest-to-guest traffic
routes through it and no loop prevention is needed.

Concurrency: everything runs on the daemon's event loop; one delivery worker
task per mesh scans for undelivered messages, coalesces bursts (``settle``),
waits for the recipient's session to go idle (up to ``busy_hold``), then
injects one fenced YAML block and advances that member's durable cursor.
"""

from __future__ import annotations

import asyncio
import base64
import bisect
import contextlib
import json
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Sequence, Tuple, Union

import yaml

from .. import atomic, digests, projects
from . import loops, mesh_ops, mesh_policy, mesh_roles, paths, wire
from .manager import AnySession, ManagerError, SessionManager
from .session import STATUS_IDLE, session_category

log = logging.getLogger("claunch.daemon.mesh")

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Who may see a mesh in another daemon's discovery list (daemon-level
#: attach, docs/mesh-design.md "Daemon attach"). ``private`` lists it
#: nowhere; ``public`` answers every relay peer's ``/peer/meshes``;
#: ``invited`` is pushed only to the daemons named in ``Mesh.offers``.
VISIBILITIES = ("private", "public", "invited")

#: The host part of an address that means "this daemon" (``dev@local``).
#: Reserved: a relay name spelled like it would make every address to that
#: daemon read as local.
LOCAL_HOST = "local"

#: Directory suffix `_drop_mesh` renames a deleted mesh to. History is kept
#: on disk deliberately, but such a directory must never be mounted again —
#: `.` is legal in a mesh name, so the suffix has to be matched exactly.
_RETIRED_RE = re.compile(r"\.deleted-\d{8}T\d{6}Z$")

#: Handle leading word -> role. The vocabulary itself lives in
#: :mod:`mesh_roles` and is per-mesh overridable; this module only ever asks
#: a *mesh* to resolve a handle (see ``Mesh.roleset``). The module-level
#: helper below is the last-resort fallback for a member record with no role
#: stored and no mesh in hand.

#: Delivered bodies are clipped at this many characters (the log keeps the
#: full text; ``history`` is the overflow path).
MAX_DELIVERY_BODY = 2000

#: Control characters stripped from message bodies at send time (tab and
#: newline survive; everything else has no business in a terminal injection).
_CTRL_RE = re.compile("[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

#: Message intents that do not invite a reply (interconnect's spec, verbatim):
#: a recipient drains ``fyi``/``ack``/``ping`` without answering — this is how
#: the mesh stops every peer from replying to every utterance. Everything
#: else — ``ask``, the default ``say``, or any custom type — invites reply.
#:
#: ``decide`` sits here for a subtler reason than the others: it *does* demand
#: something of the recipient, but the answer is recorded elsewhere (see
#: :data:`INTENT_TYPES`), so no reply can ever arrive to close the debt. Left
#: reply-expected, :meth:`Mesh.owed` would close it on the member's next
#: unrelated message and report a decision as handled that nobody made.
#: How long a delivered reply-expecting message may go unanswered before its
#: SENDER is told once. One notice, not a series: the ledger below is cleared
#: by anything the recipient says, so a watch that survives this long is one
#: nobody has spoken to at all, and a second telling of that does not make it
#: more true. Four notices at 5/10/15/20 minutes is what this replaced, and
#: one mesh had spent 424 of them on 106 watches by the time it was measured.
RESPONSE_NUDGE_AFTER = 600.0

REPLY_OPTIONAL_TYPES = frozenset({"fyi", "ack", "ping", "decide"})

#: The known message INTENTS. Other types are still accepted (and count as
#: reply-expected) but draw an advisory — see :func:`type_notice`.
#:
#: ``decide`` means: *a decision is required of you, and you record it
#: somewhere other than this thread.* Stated at the mesh level rather than as
#: a hook for the one system that sends it, because the recipient's obligation
#: is a property of the message, not of who wrote it. What is being decided
#: travels in :data:`REF_KEY`, which the mesh carries and does not interpret.
INTENT_TYPES = frozenset({"say", "ask", "decide"}) | REPLY_OPTIONAL_TYPES

#: An opaque pointer a sender may attach so a reader can follow a message back
#: to whatever it is about. The mesh stores and relays it verbatim: knowing its
#: shape would mean the mesh learning every schema that ever rides on it.
REF_KEY = "ref"
#: Key under ``ref`` that marks an urgent one-shot send and carries its record.
URGENT_REF_KEY = "urgent"
URGENT_MIN_REASON = 12
URGENT_PER_SENDER = 3
URGENT_SENDER_WINDOW = 3600.0
URGENT_PAIR_GAP = 600.0


def expects_reply(message_type) -> bool:
    """Whether a message of this ``type`` invites a reply.

    Derived on read, never stored (interconnect's convention) — the on-disk
    message model is unchanged and messages written before ``type`` existed
    still classify correctly.
    """
    return str(message_type or "say").strip().lower() not in REPLY_OPTIONAL_TYPES


def type_notice(message_type) -> Optional[str]:
    """Advise when ``type`` is not a known intent (usually a role leaked in).

    Non-blocking: an unrecognized type counts as reply-expected and so
    quietly invites a reply-all the author probably did not intend.
    """
    if str(message_type or "say").strip().lower() in INTENT_TYPES:
        return None
    return (
        f"type {message_type!r} is not a known intent (ask/say/fyi/ack/decide) "
        "— it counts as reply-expected. 'type' is the message INTENT, not your "
        "role or a label; use 'fyi'/'ack' for no-reply status so peers "
        "don't reply-all."
    )


def msg_type_for(msg: dict, handle: str) -> str:
    """The intent ``handle`` experiences: its section's type, else the top one."""
    sec = (msg.get("sections") or {}).get(handle)
    if isinstance(sec, dict) and sec.get("type"):
        return str(sec["type"])
    return str(msg.get("type") or "say")


def _slice_body(shared: str, section_text: Optional[str]) -> str:
    """A recipient's slice: the shared preamble and its own section."""
    return "\n\n".join(p for p in (shared, section_text) if p)


def recipient_body(msg: dict, handle: str) -> str:
    """The text ``handle`` actually receives: for a batch, the shared preamble
    plus its OWN section only — never another member's instructions. The log
    keeps the composite, so anything showing a message back to (or about) one
    recipient must slice it the same way delivery did."""
    if msg.get("sections") is not None:
        sec = (msg.get("sections") or {}).get(handle)
        return _slice_body(
            str(msg.get("shared") or ""),
            sec.get("text") if isinstance(sec, dict) else None,
        )
    return str(msg.get("body") or "")


#: The words :func:`MeshManager.mesh_info` accepts for ``state``. The same
#: partition the roster's filter bar draws, named the same way, because a
#: filter the page applies and a filter the daemon applies must agree about
#: what "current" means.
MEMBER_STATES = (
    "all", "current", "running", "remote",
    "killed", "paused", "archived", "missing",
)


def member_in_state(category: str, state: str) -> bool:
    """Is a member of ``category`` in the roster ``state`` asked for?

    ``remote`` counts as current: another daemon's member has no local record
    and its death is never reported here, so unknown is not dead. Hiding a
    member that is working is the expensive way to be wrong.
    """
    if state == "all":
        return True
    if state == "current":
        return category in ("running", "remote")
    return category == state


def _age_secs(ts, now: datetime) -> Optional[float]:
    """Seconds since an ISO ``ts``; None if it is missing or unparsable (a
    message from a future/older daemon must not sink the whole report)."""
    try:
        then = datetime.fromisoformat(str(ts))
    except (TypeError, ValueError):
        return None
    if then.tzinfo is None:
        then = then.replace(tzinfo=timezone.utc)
    return max(0.0, (now - then).total_seconds())


def _composite_body(shared: str, sections: dict) -> str:
    """The full batch text kept in the log: shared + every @handle section."""
    parts = [shared] if shared else []
    parts += [f"@{h}: {sec['text']}" for h, sec in sections.items()]
    return "\n\n".join(parts)


def _separable_notice(body: str, recipients: List[str]) -> Optional[str]:
    """Nudge toward a BATCH send when one body @-addresses several recipients."""
    if len(recipients) < 2 or not body:
        return None
    tagged = [
        r for r in recipients if re.search(rf"@{re.escape(r)}(?![\w-])", body)
    ]
    if len(tagged) < 2:
        return None
    return (
        f"this one body @-addresses {len(tagged)} recipients "
        f"({', '.join(tagged)}) — that looks like per-recipient content. Send "
        "it as a BATCH (sections={'<handle>': '<their part>'}) so each peer "
        "reads only its own slice plus the shared body."
    )


#: A one-line shared preamble at or under this many characters is a routing
#: header ("read your own section"), not an announcement that stands on its
#: own. Measured over the 197 batch sends in this machine's mesh logs: every
#: preamble that reached a non-sectioned member as its entire message was one
#: line and 91 characters or fewer, and every one-line preamble that did read
#: as a message on its own was 208 characters or more, with a single
#: exception at 72. So the two populations do not separate by length alone
#: below ~200; 120 clears the observed header range with margin and keeps the
#: advisory off the substantive one-liners.
_THIN_PREAMBLE_CHARS = 120


def _preamble_only_notice(
    shared: str, sections: dict, recipients: List[str]
) -> Optional[str]:
    """Warn when a batch's non-sectioned recipients receive a header.

    A section-bearing send still goes to every recipient in ``to``; one with
    no section of its own is delivered the shared preamble alone. That is
    correct when the preamble is a real announcement, and it wakes a terminal
    for nothing when the preamble was written as a heading for the sectioned
    members ("each of you read your own part"). The sender cannot tell the
    two apart from its own screen, which shows the composite — so name who
    receives only this, and how little it is.
    """
    uncovered = [r for r in recipients if r not in sections]
    if not uncovered:
        return None
    text = shared.strip()
    if len(text.splitlines()) > 1 or len(text) > _THIN_PREAMBLE_CHARS:
        return None
    return (
        f"{len(uncovered)} recipient(s) with no section ({', '.join(uncovered)}) "
        f"receive ONLY the {len(text)}-character shared preamble as their whole "
        "message. If that preamble is a heading for the sectioned members "
        "rather than an announcement, they are being woken with no content: "
        "give them a section, drop them from 'to', or put the announcement "
        "itself in the shared body."
    )


#: Audience selectors a sender may put in ``to`` instead of naming handles:
#: selector -> the board status whose assignees it resolves to. Resolved once,
#: at send time, into an explicit handle list — the log records who the
#: message was actually for, and delivery never re-reads the board.
#: ``@in_review`` exists for the leader's baseline notice: its only audience
#: is the sessions that have already put numbers up for landing
#: (claunch-424v4, claunch-no-premature-rebase-lhcz).
AUDIENCE_SELECTORS: Dict[str, str] = {"@in_review": "in_review"}


def broadcast_notice(recipients: List[str]) -> str:
    """The advisory every agent ``'*'`` send carries.

    ``'*'`` reaches every connected member whatever it is doing — including
    sessions whose work already landed and that sit waiting for a person to
    end them — and each of them spends a turn reading it. Measured on
    mesh-0826: of 33 recipients of one leader broadcast whose run position
    could be read, 31 had already finished (claunch-424v4).
    """
    return (
        f"BROADCAST: '*' typed this into {len(recipients)} terminal(s), and "
        "each spends a turn on it whether or not it concerns them — finished "
        "sessions waiting to be ended included. Address the members it is "
        "for: their handles, or an audience selector ("
        + ", ".join(sorted(AUDIENCE_SELECTORS))
        + " = the sessions whose issue is in that board state). Keep '*' for "
        "an announcement every member must act on."
    )


#: What ``exited`` and ``missing`` mean to a sender, and what to do about
#: each. Split because the two need opposite actions: an exited session is
#: waiting to be respawned, a missing one has had its record cleared and
#: never will be.
_STRANDED_WHAT = {
    "exited": (
        "session {session!r} has exited — nothing is reading that terminal. "
        "The message is QUEUED, not delivered, and stays queued until that "
        "session is respawned"
    ),
    "missing": (
        "session {session!r} is gone from the registry — the message is "
        "queued against a session nothing can bring back"
    ),
}


def stranded_notice(entries: List[dict]) -> Optional[str]:
    """What to tell a sender whose recipients cannot read anything.

    Delivery holds the cursor for an exited member and says nothing (see
    :meth:`MeshManager._deliver_to`) — right for the message, wrong for the
    sender, who is told ``recipients: [bob]`` and goes on typing into a
    terminal that no longer exists. This is the sentence that stops that.

    It deliberately does NOT offer to respawn: reviving a session costs a
    real terminal and a real agent's context, and most stranded messages are
    a report the sender no longer needs. So the hint is paired with the
    condition under which it is worth spending — the judgement stays with
    whoever knows what the message was for.
    """
    if not entries:
        return None
    parts = []
    for e in entries:
        what = _STRANDED_WHAT.get(e.get("state") or "", _STRANDED_WHAT["exited"])
        parts.append(f"{e['handle']}: " + what.format(session=e["session"]))
    revivable = [e for e in entries if e.get("state") == "exited"]
    tail = (
        " Revive it ONLY if this message must actually land: "
        + "; ".join(
            mesh_policy.RESUME_HINT.format(session=e["session"]) for e in revivable
        )
        + ". Otherwise stop sending there — take the work to a live peer, or "
        "spawn a replacement."
        if revivable
        else " Drop the handle from the mesh, or route the work to a live peer."
    )
    return ". ".join(parts) + "." + tail


def _busy_notice(entries: List[dict]) -> str:
    """What to tell a sender whose recipients are too far behind to accept.

    The counterpart of :func:`stranded_notice`, for the opposite problem: a
    stranded recipient has no terminal, a congested one has a terminal with
    more waiting for it than a turn can act on. Both are the same surprise
    from where the sender stands — ``sent`` came back and nothing happened —
    and both are only fixable by saying so at send time.

    Written to be acted on by an agent reading it in its own terminal: what
    did not happen, why, and the ONE thing to do about it. It says "wait"
    rather than "retry", because an agent told to retry retries at once, and
    a burst of retries against a congested member is the flood this gate
    exists to stop.
    """
    who = ", ".join(
        f"{e['handle']} ({e['queued']} waiting, cap {e['inbox_max']})"
        for e in entries
    )
    # Split on whether WAITING is the remedy, not on the reason name: a
    # reason the sender cannot wait out needs its own sentence, and keying
    # the other branch off "everything that is not a delivery hold" put
    # every such reason back into the wait bucket it does not belong in.
    wait = max(
        (
            e.get("retry_after") or 0.0
            for e in entries
            if not e.get("reason")
        ),
        default=0.0,
    )
    actions = []
    if any(e.get("reason") == "delivery_hold" for e in entries):
        actions.append("wait until delivery resumes for held receivers")
    # Named per state because the remedies are not interchangeable, and a
    # respawn costs a real terminal and a real agent's context — the same
    # judgement stranded_notice() leaves with the sender.
    exited = [e["handle"] for e in entries if e.get("reason") == "exited"]
    if exited:
        actions.append(
            "respawn " + ", ".join(exited) + " if the message must land, "
            "or take the work to a live peer"
        )
    missing = [e["handle"] for e in entries if e.get("reason") == "missing"]
    if missing:
        actions.append(
            "route around " + ", ".join(missing)
            + " — nothing can bring those sessions back"
        )
    if wait:
        actions.append(f"wait about {int(wait)}s for other receivers")
    action = "; ".join(actions) or "wait before sending there again"
    # First letter only. ``str.capitalize`` lowercases the rest, which was
    # harmless while every action began with "wait" and destroys a handle's
    # spelling now that one can begin with a member's name.
    action = action[:1].upper() + action[1:]
    return (
        f"NOT DELIVERED to {who}: that terminal has not read what it already "
        f"has, so the mesh is not accepting more for it. {action}, "
        "then re-send. Nothing was queued: this message "
        "does not exist anywhere and will not arrive on its own."
    )


def _normalize_sections(
    sections, recipients: List[str], sender: str
) -> Optional[dict]:
    """Validate/normalize a batch ``sections`` map, or None if absent.

    Every key must be an actual recipient of this send (not the sender, not
    someone left out of ``to``) so a section can never be silently
    undeliverable — interconnect's contract, verbatim.
    """
    if not sections:
        return None
    if not isinstance(sections, dict):
        raise MeshError(
            "sections must be a mapping of handle -> text (or handle -> {text, type})"
        )
    rcpt_set = set(recipients)
    norm: dict = {}
    for h, v in sections.items():
        h = str(h)
        if isinstance(v, str):
            sec = {"text": v}
        elif isinstance(v, dict):
            text = v.get("text")
            if not isinstance(text, str) or not text.strip():
                raise MeshError(f"sections[{h!r}] needs a non-empty 'text'")
            sec = {"text": text}
            if v.get("type"):
                sec["type"] = str(v["type"]).strip().lower()
        else:
            raise MeshError(
                f"sections[{h!r}] must be a string or a {{text, type}} object"
            )
        sec["text"] = _CTRL_RE.sub("", sec["text"]).strip()
        if not sec["text"]:
            raise MeshError(f"sections[{h!r}] needs a non-empty 'text'")
        if h == sender:
            raise MeshError("a section cannot target the sender")
        if h not in rcpt_set:
            raise MeshError(
                f"sections names {h!r}, who is not a recipient of this send — "
                "add them to 'to' (or use '*'), or drop the section"
            )
        norm[h] = sec
    return norm


#: Wait for a message burst to go quiet before delivering (seconds).
DEFAULT_SETTLE = 2.0

#: How long to hold delivery for a busy session before injecting anyway —
#: harnesses like claude queue text typed during a turn, so this is safe; the
#: hold just avoids interleaving with short turns. Seconds.
DEFAULT_BUSY_HOLD = 60.0

#: How long a refusal stays on a member's record, and how many are kept.
#: Long enough for a person who walks back to the dashboard to see that the
#: quiet terminal was quiet because the mesh was turning senders away, short
#: enough that it says "right now" rather than "at some point today".
_REFUSED_WINDOW = 600.0
_REFUSED_KEEP = 40

#: Longest stance the join briefing will paste inline, for the members whose
#: system prompt does not carry one (see :meth:`MeshManager._stance_lines`).
#: Set to fit every PACKAGED stance whole — the leader's is much the longest
#: at ~3.8k, and it is also the one a truncation would hurt most, since a
#: leader that never read its stance is the failure this whole path is about.
#: ``test_every_packaged_stance_fits_the_inline_cap_whole`` is what keeps the
#: two in step when either moves.
#:
#: Still a cap, because ``mesh_roles.MAX_STANCE`` is 8000: a custom
#: vocabulary that writes a novel gets a starting position and the pointer to
#: the rest rather than pushing the roster out of the block it rides in. The
#: re-briefing has a harder budget than the join does and drops back to the
#: pointer instead of trimming anything else (see :func:`rebrief.compose`).
_INLINE_STANCE = 4000

#: Worker rescan cadence while messages are pending (seconds).
_POLL = 1.0

#: Peer flush retry backoff after a failure (seconds, doubling to the cap).
_PEER_BACKOFF_BASE = 5.0
_PEER_BACKOFF_MAX = 60.0


def utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def infer_role(handle: str) -> str:
    """The role a handle self-selects under the PACKAGED vocabulary.

    Only for a member record that reaches us with no role at all (very old
    state, a hand-edited ``mesh.json``). Everything on the join path resolves
    through the mesh's own set instead — see ``MeshManager._resolve_role`` —
    because that is the one a mesh may have overridden.
    """
    return mesh_roles.resolve().infer(handle)


class MeshError(Exception):
    """Raised for unknown meshes/members and invalid mesh operations."""


class MeshConflict(MeshError):
    """Raised when a mesh or handle already exists (HTTP 409)."""


class MeshBusy(MeshError):
    """Every recipient of a send is too far behind to accept it (HTTP 429).

    Backpressure, and the one refusal in this module that is not about the
    message being wrong. The mesh has no way to make a terminal read faster,
    so the only honest thing it can do when a member's backlog is already
    deeper than a turn can act on is stop taking more and say so — to the
    SENDER, synchronously, while it is still standing there and can decide
    what to do instead. A queue that only ever grows would have accepted the
    same message and told the sender ``sent``.

    ``entries`` is one ``{handle, queued, inbox_max, retry_after, remote}``
    per refused recipient (see
    :meth:`MeshManager.congested_recipients`), so a caller can mark the row
    rather than parse the sentence; ``retry_after`` is the soonest of them,
    in seconds, and is advisory — nothing enforces the wait.
    """

    def __init__(self, message: str, entries: List[dict], retry_after: float):
        super().__init__(message)
        self.entries = entries
        self.retry_after = retry_after


class PeerUnreachable(MeshError):
    """A peer call failed at the *transport* level (relay down, bridge broken).

    Distinct from an application-level rejection: an unreachable primary means
    a send may be queued durably, while a rejection (bad handle, bad token)
    must surface immediately and never queue.
    """


class Member:
    def __init__(
        self,
        handle: str,
        session: str,
        *,
        machine: str = "",
        role: str = "",
        subroles: Optional[Sequence[str]] = None,
        joined_at: str = "",
        wired: bool = False,
    ) -> None:
        self.handle = handle
        self.session = session
        self.machine = machine  # "" = this daemon; set by federation later
        self.role = role or infer_role(handle)
        #: Further roles this member HOLDS besides :attr:`role`, in the order
        #: they were given. The primary role stays the one thing a member IS —
        #: its stance, its briefing, the label the roster prints first — while
        #: a subrole is a role it also answers for: every lookup that asks
        #: "who holds role X" (a delegated decision's candidates, a workflow's
        #: ``filter_roles``, the policy engine's watchers and polled roles,
        #: an exclusive role's live holder, an auto-link rule) reads
        #: :attr:`roles`, which is the primary followed by these. Resolved at
        #: join through the mesh's vocabulary exactly like the primary and
        #: stored as plain names; a role-set upload never rewrites them.
        self.subroles: List[str] = _dedupe_roles(subroles or (), skip=self.role)
        self.joined_at = joined_at or utcnow()
        #: This member's edges were decided by its join (see
        #: ``MeshManager._wire_member``), so a pair with no recorded edge is
        #: CLOSED for it — where for everyone else an unrecorded pair is open.
        #:
        #: That inversion has to be per member rather than per mesh, and this
        #: flag is the whole reason why: a mesh that predates the wiring keeps
        #: the complete graph it has always had, and nothing migrates. A member
        #: arriving from an older daemon has no flag in its roster entry and
        #: reads as False here, which is the same answer.
        self.wired = bool(wired)

    @property
    def local(self) -> bool:
        return not self.machine

    @property
    def roles(self) -> List[str]:
        """Every role this member holds: the primary first, then its subroles.

        The one list a role lookup should read. Comparing ``member.role`` alone
        asks "what is this member", which is the right question for its stance
        and nothing else; "does it hold role X" is ``X in member.roles``.
        """
        return [self.role, *self.subroles]

    def holds(self, role: str) -> bool:
        return str(role or "").strip().lower() in self.roles

    def role_label(self) -> str:
        """``leader+reviewer`` — the roster's one-word spelling of the set."""
        return "+".join(self.roles)

    def to_dict(self) -> dict:
        return {
            "handle": self.handle,
            "session": self.session,
            "machine": self.machine,
            "role": self.role,
            "subroles": list(self.subroles),
            # Derived, and published so every reader of a roster row — the
            # cflow pool, a mirror, the CLI — asks "holds X?" against one
            # list instead of re-deriving it. `from_dict` ignores it.
            "roles": self.roles,
            "joined_at": self.joined_at,
            "wired": self.wired,
        }

    @classmethod
    def from_dict(cls, doc: dict) -> "Member":
        return cls(
            str(doc["handle"]),
            str(doc.get("session") or ""),
            machine=str(doc.get("machine") or ""),
            role=str(doc.get("role") or ""),
            # A record written by a daemon that predates subroles carries no
            # key, and reads as a member of its primary role alone.
            subroles=_read_subroles(doc.get("subroles")),
            joined_at=str(doc.get("joined_at") or ""),
            wired=bool(doc.get("wired")),
        )


def _read_subroles(raw) -> List[str]:
    """A ``subroles`` value as it arrives in a document or a request body.

    A list of names is the shape; a comma-separated string is accepted from
    a hand-typed body. Anything else reads as no subroles rather than as an
    error, because this runs on records that crossed a federation link from
    a daemon whose schema this build may not know.
    """
    if isinstance(raw, str):
        raw = raw.split(",")
    if not isinstance(raw, (list, tuple)):
        return []
    return [str(r).strip().lower() for r in raw if str(r or "").strip()]


def _dedupe_roles(names: Sequence[str], *, skip: str = "") -> List[str]:
    """Order-preserving unique lower-cased names, without ``skip``."""
    out: List[str] = []
    for name in names:
        name = str(name or "").strip().lower()
        if name and name != skip and name not in out:
            out.append(name)
    return out


class _LogIndex:
    """Who each log entry is addressed to and who sent it, by log position.

    ``pending`` and ``owed_all`` used to walk the log member by member, and
    almost every entry they visited was addressed to someone else: on
    mesh-0826 (382 members, 29697 messages) one pass over every member read
    4.3 million entries to find the few thousand that concerned anyone, 4.1s
    of event loop per full roster (measured 2026-09-24). This keeps the
    positions that can concern a handle -- sent to it by name, or to ``"*"``
    -- so those walks visit only them.

    It indexes the ADDRESS, never the answer: whether an entry reaches a
    member still goes through :meth:`Mesh.addressed_to`, so the member graph
    stays current exactly as before. The log is append-only and its entries
    are never re-addressed, so the index only ever extends; a log that was
    replaced or shortened (a reload, a test) is noticed by its identity and
    its last entry, and rebuilt.
    """

    __slots__ = ("log", "size", "last", "to", "everyone", "last_from")

    def __init__(self) -> None:
        self.log: Optional[list] = None
        self.size = 0
        self.last: Optional[dict] = None
        #: handle -> ascending positions naming it in ``to``.
        self.to: Dict[str, List[int]] = {}
        #: ascending positions addressed to ``"*"``.
        self.everyone: List[int] = []
        #: handle -> the last position it sent.
        self.last_from: Dict[str, int] = {}

    def sync(self, log: list) -> None:
        n = len(log)
        if (
            log is not self.log
            or n < self.size
            or (self.size and log[self.size - 1] is not self.last)
        ):
            self.__init__()
            self.log = log
        for k in range(self.size, n):
            m = log[k]
            sender = m.get("from")
            if isinstance(sender, str):
                self.last_from[sender] = k
            to = m.get("to")
            if to == "*":
                self.everyone.append(k)
            elif isinstance(to, list):
                for h in {h for h in to if isinstance(h, str)}:
                    self.to.setdefault(h, []).append(k)
            elif isinstance(to, str):
                self.to.setdefault(to, []).append(k)
        self.size = n
        self.last = log[n - 1] if n else None

    def candidates(self, handle: str, lo: int) -> List[int]:
        """Ascending positions ``>= lo`` that may be addressed to ``handle``."""
        mine = self.to.get(handle) or []
        a = mine[bisect.bisect_left(mine, lo):]
        b = self.everyone[bisect.bisect_left(self.everyone, lo):]
        if not b:
            return a
        if not a:
            return b
        return sorted(a + b)


class Mesh:
    """One mesh: membership, its message log, and per-member delivery state."""

    @property
    def name(self) -> str:
        """The local key: the bare name for a mesh this daemon created (the
        ``@local`` that may be left off), ``name@origin`` for one created
        elsewhere — so a local ``dev`` and ``dev@pca`` can both live here.
        See docs/mesh-design.md "Mesh addresses"."""
        return f"{self.wire_name}@{self.origin}" if self.origin else self.wire_name

    def __init__(
        self, name: str, *, created_at: str = "", me: str = "", project: str = "",
        origin: str = "",
    ) -> None:
        #: The mesh's name as every daemon knows it — what peer calls carry.
        self.wire_name = name
        #: The daemon that created the mesh (its relay name), "" when that is
        #: this daemon. Fixed for the mesh's life: authority may move (phase
        #: 7) but the address ``wire_name@origin`` does not.
        self.origin: str = origin
        self.created_at = created_at or utcnow()
        #: The project this mesh is filed under (see
        #: :mod:`claude_launcher.projects`). ``""`` = the default project,
        #: which is what every mesh.json written before the field existed
        #: reads as — nothing on disk moves.
        self.project: str = "" if projects.normalize(project) == projects.DEFAULT else projects.normalize(project)
        self.members: Dict[str, Member] = {}
        self.messages: List[dict] = []  # in-memory mirror of log.jsonl
        self._log_index = _LogIndex()  # see _indexed()
        self.cursors: Dict[str, int] = {}  # handle -> delivered log index
        #: This daemon's relay name, mirrored from MeshManager.machine so the
        #: mesh can work out its own rank. "" = no relay identity yet.
        self.me: str = me
        #: Phase 7: the mesh's daemons in RANK order — ``peers[0]`` is the
        #: authority (sequencer, roster owner, policy engine) and every
        #: further entry is an ordinary peer. Empty = a purely local mesh
        #: that has never federated.
        self.peers: List[str] = []
        #: One entry per linked peer machine:
        #: ``{token_in, token_out, created_at, enabled}``. ``token_in``
        #: authenticates that peer's calls to us, ``token_out`` is what we
        #: present to it — the pair is exchanged by the link handshake, so
        #: every edge is duplex. ``enabled`` False = the operator cut it.
        self.links: Dict[str, dict] = {}
        #: Authority side: brokered credentials for the peer-to-peer edges
        #: it is not part of, ``"m1|m2"`` (sorted) -> {token_12, token_21,
        #: created_at, enabled}. The authority already holds an
        #: authenticated channel to both ends, so it mints both halves and
        #: ships each side its own view — no separate handshake, and no
        #: daemon has to trust an unauthenticated first contact.
        self.pair_links: Dict[str, dict] = {}
        #: Whether each peer-to-peer edge is live, ``"m1|m2"`` (sorted) ->
        #: enabled. The authority owns it (an operator cuts an edge there)
        #: and ships it to everyone, so every daemon can draw the same
        #: graph — including edges it is not itself an endpoint of.
        self.edges: Dict[str, bool] = {}
        #: Whether two *members* may message each other, ``"h1|h2"`` (sorted
        #: handles) -> enabled. A recorded edge always wins; what a MISSING key
        #: means depends on the two members (see :meth:`connected`) — closed if
        #: either was wired by its join, connected otherwise. The second answer
        #: is the original convention, the one ``edges`` still uses one layer
        #: down, and it is why a mesh that predates the wiring stays the
        #: complete graph it has always been with nothing to migrate.
        #:
        #: Unlike ``edges`` this is a hard ACL, not a fast-path hint: there is
        #: no multi-hop routing between members either, so a cut here has
        #: nowhere to fall back to and a send across it is refused. Owned by
        #: the authority and shipped to every peer, so a guest cannot let
        #: through what the authority forbids.
        self.member_edges: Dict[str, bool] = {}
        #: Standing asks for an edge this graph does not have, pair key ->
        #: :class:`wire.WireRequest`. Written when a send is refused for want
        #: of a connection (see :meth:`MeshManager.file_wire_request`), so the
        #: one moment an agent names the peer it needs is recorded instead of
        #: being spent on a refusal string.
        #:
        #: Persisted, like ``pending_requests`` and for the same reason: an
        #: ask nobody has answered yet is exactly the thing a restart must not
        #: drop. Losing one puts the requester back to waiting on a channel
        #: that will never be decided, which is the outcome this whole path
        #: exists to prevent.
        self.wire_requests: Dict[str, "wire.WireRequest"] = {}
        #: Authority side: the coordination leases members hold on shared
        #: keys (a file path, an issue id) — see :mod:`mesh_ops`. Kept by
        #: ``peers[0]`` only, because a lock two daemons could each grant is
        #: no lock; a peer forwards acquire/release up the link exactly like
        #: a send. Persisted in ``leases.json`` so a restart of the authority
        #: does not silently free every key mid-edit.
        self.leases = mesh_ops.LeaseRegistry()
        #: Bumped on every authority handover; messages carry it alongside
        #: ``seq`` so a forced takeover cannot silently interleave with the
        #: old authority's late traffic.
        self.authority_epoch: int = 0
        #: Authority side: next sequence number to hand out.
        self.next_seq: int = 0
        #: Fast-path arrivals that have been injected into local terminals
        #: but not yet sequenced by the authority. Folded into ``messages``
        #: when the sequenced copy syncs in.
        self.provisional: List[dict] = []
        #: handle -> message ids already injected (via the fast path) that
        #: still sit at or beyond the member's cursor, so folding a
        #: provisional message into the log never re-injects it.
        self.delivered_ids: Dict[str, set] = {}
        #: handle -> message ids the OPERATOR has written off: mail that was
        #: delivered and never answered, and never will be, because the human
        #: watching the dashboard settled it by hand. Excluded from
        #: :meth:`owed` — see there for why this is the operator's verdict
        #: and not the member's reply. Persisted next to the cursors, since
        #: it is per-member delivery state of exactly that kind.
        self.dismissed: Dict[str, set] = {}
        #: Reply-expecting deliveries without a threaded ack/reply.  This is
        #: a delivery receipt, so it persists with cursors across restarts.
        self.response_watches: Dict[str, dict] = {}
        # Recipient -> senders already warned during this absence. Persisted
        # with cursors so restarting the daemon does not repeat the warning.
        self.stranded_told: Dict[str, List[str]] = {}
        #: Outstanding invite tickets (authority only; pre-approval for a
        #: join request): token -> minted-at ISO timestamp. TTL-checked at
        #: redemption (MeshManager.invite_ttl).
        self.invites: Dict[str, str] = {}
        #: Authority side: who may discover this mesh (see VISIBILITIES).
        self.visibility: str = "private"
        #: Authority side: daemons this mesh was offered to, machine ->
        #: {token, created_at}. The token lets that machine attach without
        #: approval; it lives until the offer is cancelled or redeemed.
        self.offers: Dict[str, dict] = {}
        #: This daemon's relay name changed: ``{old, pending: [machines]}``
        #: — the peers not told yet (``/peer/mesh/renamed``). Persisted so a
        #: restart before every peer heard it keeps telling them.
        self.rename_notice: Optional[dict] = None
        #: Primary side: codeless join requests awaiting operator approval,
        #: id -> {id, machine, session, handle, role, reply_token,
        #: requested_at}. Persisted so approvals survive a restart.
        self.pending_requests: Dict[str, dict] = {}
        #: Primary side: approved joins whose grant callback has not reached
        #: the guest yet, id -> {machine, handle, reply_token}.
        self.pending_grants: Dict[str, dict] = {}
        #: Authority side: per-peer fanout cursor into ``messages``.
        self.link_cursors: Dict[str, int] = {}
        #: Runtime peer call status: machine -> {ok, error, retry_at, backoff,
        #: last_sync, roster_seen}. On a mirror the single key is the primary.
        self.peer_status: Dict[str, dict] = {}
        #: Mirror side: durable upstream queue of sends the primary has not
        #: accepted yet (persisted in outbox.jsonl; drains strictly in order).
        self.outbox: List[dict] = []
        #: Primary side: freshest activity report per remote member handle
        #: (piggybacked on guest sync acks; read by the policy tick).
        self.remote_activity: Dict[str, dict] = {}
        #: Parent handle per member hosted somewhere else. Lineage is a fact
        #: about a *session*, so only the daemon running it can derive one;
        #: this is what the rest of the mesh is told. Kept beside the roster
        #: rather than on Member because it stays derived at the source — a
        #: parent stored on a member would be a second truth to keep in step.
        self.remote_lineage: Dict[str, str] = {}
        #: Primary side: policy nudge instructions awaiting fanout,
        #: machine -> [{handle, kind, body}].
        self.pending_nudges: Dict[str, List[dict]] = {}
        #: Bumped on every roster change; per-guest ``roster_seen`` in
        #: peer_status decides whether a sync must carry the roster urgently.
        self.roster_version: int = 0
        self.seen_ids: set = set()  # message-id dedupe (idempotent redelivery)
        #: Delivery policy config — the three nudges (heartbeat /
        #: task-poll / stall warnings) plus the backpressure gate that
        #: bounds what any of them can hand a terminal. Persisted in
        #: mesh.json and edited via the API/web.
        self.policy: dict = mesh_policy.default_policy()
        #: This mesh's role-set OVERRIDE (None = the packaged vocabulary), as
        #: uploaded to the authority. Persisted in mesh.json and federated,
        #: so every daemon in the mesh resolves handles the same way.
        self.roles_doc: Optional[dict] = None
        #: Bumped whenever ``roles_doc`` changes. A guest's ``roles_seen``
        #: decides whether a sync must carry the (comparatively fat) role set,
        #: so stance text does not ride every message flush.
        self.roles_version: int = 0
        #: Cache of ``roles_doc`` resolved against the packaged default, with
        #: the version it was built from — resolving parses YAML, and the
        #: join path must not pay that per member.
        self._roleset: Optional[mesh_roles.RoleSet] = None
        self._roleset_version: int = -1
        #: In-memory per-member activity/timers the policy tick reads:
        #: handle -> {anchor, last_sent, last_delivered, hb_/tp_/warn_ timers}.
        self.activity: Dict[str, dict] = {}
        self.wake = asyncio.Event()
        self.last_append = 0.0  # monotonic time of the last log append
        self._first_pending: Dict[str, float] = {}  # handle -> monotonic
        #: handle -> (len(messages), len(provisional)) at the last delivery
        #: tick that found the member's session gone. The tick skips the
        #: member while nothing has been appended since: a stranded backlog
        #: never drains by itself, and rescanning the log from a dead
        #: member's cursor every second is what a 16k-message mesh with 170
        #: exited members spends half the daemon's CPU on (claunch-qx5c).
        self._stranded_scan: Dict[str, Tuple[int, int]] = {}
        #: Retired primary/mirror state read off disk, held until the relay
        #: name is known so it can be folded into ``peers`` (see
        #: MeshManager._migrate_v2). None once migrated or not applicable.
        self._v2: Optional[dict] = None

    # ------------------------------------------------------------------ #
    # roles
    # ------------------------------------------------------------------ #
    @property
    def roleset(self) -> mesh_roles.RoleSet:
        """The vocabulary in force here: the packaged set plus our override.

        An override that no longer resolves (written by a newer daemon, say)
        falls back to the packaged set rather than breaking every join — the
        mesh keeps working with a vocabulary everyone understands.
        """
        if self._roleset is None or self._roleset_version != self.roles_version:
            try:
                self._roleset = mesh_roles.resolve(self.roles_doc)
            except mesh_roles.RoleError as exc:
                log.warning(
                    "mesh %r: unusable role set (%s) — falling back to the "
                    "packaged vocabulary", self.name, exc,
                )
                self._roleset = mesh_roles.resolve()
            self._roleset_version = self.roles_version
        return self._roleset

    def set_roles_doc(self, doc: Optional[dict], *, version=None) -> bool:
        """Adopt a role-set override. Returns whether anything changed.

        The authority bumps the version itself; a mirror adopts the number the
        authority reports, so ``roles_version`` means the same thing mesh-wide
        and a guest can tell whether it is holding the current vocabulary.
        """
        if doc == self.roles_doc and version in (None, self.roles_version):
            return False
        self.roles_doc = doc
        if version is None:
            self.roles_version += 1
        else:
            try:
                self.roles_version = int(version)
            except (TypeError, ValueError):
                self.roles_version += 1
        return True

    # ------------------------------------------------------------------ #
    # rank
    # ------------------------------------------------------------------ #
    @property
    def authority(self) -> str:
        """The machine that sequences this mesh — ``peers[0]``.

        A mesh that never federated has no peer list; we are its authority
        by construction.
        """
        return self.peers[0] if self.peers else self.me

    @property
    def primary(self) -> str:
        """Legacy view of :attr:`authority`: ``""`` when *we* hold it.

        Phase 7 turned ownership into a position, but "am I the authority?"
        is asked all over this module and reads best as ``if mesh.primary``.
        """
        auth = self.authority
        return "" if not auth or auth == self.me else auth

    def rank(self, machine: str) -> int:
        """Rank of ``machine`` (0 = authority); -1 when it is not a peer."""
        try:
            return self.peers.index(machine)
        except ValueError:
            return -1

    def owns_link(self, machine: str) -> bool:
        """Whether WE drive the handshake on the edge to ``machine``.

        The lower-ranked side owns the edge. An unranked side (no relay
        identity yet, or a peer we only just heard of) never owns it.
        """
        mine, theirs = self.rank(self.me), self.rank(machine)
        if mine < 0:
            return False
        return theirs < 0 or mine < theirs

    def linked(self, machine: str) -> bool:
        """A live (uncut) credential pair exists toward ``machine``."""
        link = self.links.get(machine)
        return bool(link) and link.get("enabled", True)

    # ------------------------------------------------------------------ #
    # member graph
    # ------------------------------------------------------------------ #
    @staticmethod
    def member_key(a: str, b: str) -> str:
        """The canonical key for a member pair — sorted, so one edge has one
        key whichever end asks about it."""
        return "|".join(sorted((a, b)))

    def connected(self, a: str, b: str) -> bool:
        """May ``a`` and ``b`` message each other?

        Three answers, in the order they are asked for:

        1. **A recorded edge wins.** Somebody decided this pair — a join's
           wiring, an agent connecting its workers, a human cutting a link —
           and a decision outranks any default.
        2. **Otherwise, a wired member is closed.** A member whose join
           decided its edges (:attr:`Member.wired`) has exactly the edges that
           join recorded; a pair nobody wrote down is one nobody wanted. This
           is what lets a spawned child be connected to its parent *and to
           nothing else* while storing a single edge rather than a cut against
           every member who happened to be present.
        3. **Otherwise, open.** The original convention, kept for every member
           that predates the wiring, so an existing mesh stays the complete
           graph it has always been and nothing migrates.

        A member is always 'connected' to itself: self-addressed sends are
        rejected elsewhere (a sender is never its own recipient), and
        answering False here would make that read as a topology error.

        A handle that is not a member at all is connected to everyone, because
        it is not in this graph to be cut from it: an external sender is the
        operator at a CLI or a dashboard (or the policy engine), and the member
        graph governs what the members may do, not what may be said to them.
        """
        if a == b:
            return True
        recorded = self.member_edges.get(self.member_key(a, b))
        if recorded is not None:
            return bool(recorded)
        ends = (self.members.get(a), self.members.get(b))
        if any(m is None for m in ends):
            return True
        return not any(m.wired for m in ends)

    def neighbours(self, handle: str) -> List[str]:
        """Every other member ``handle`` may talk to, in handle order."""
        return sorted(
            h for h in self.members if h != handle and self.connected(handle, h)
        )

    def member_edge_table(self, only: Optional[Iterable[str]] = None) -> List[dict]:
        """Every member pair with its state — what a topology view needs.

        Emitted for all pairs, not just the cut ones, because "connected" is
        a default rather than a stored fact: a caller handed only the cut set
        would have to know the default to draw the graph, and the two answers
        would drift the first time the default changed.

        ``only`` narrows it to pairs where both ends are in that set, which is
        the table for a roster that was filtered: an edge is only as visible
        as both of its ends, and a reader draws no line to a member it was
        not sent. The count is quadratic, so the narrowing is what keeps a
        filtered answer small -- 251 members is 31375 pairs and 1.6MB, and
        the eight running ones are 28 pairs (mesh-0826, 2026-09-20).
        """
        handles = sorted(self.members if only is None else set(only) & set(self.members))
        return [
            {"a": a, "b": b, "enabled": self.connected(a, b)}
            for i, a in enumerate(handles)
            for b in handles[i + 1:]
        ]

    def isolate(self, handle: str, *, keep: Iterable[str] = ()) -> List[str]:
        """Cut ``handle`` off from every current member except ``keep``.

        A blunt instrument for an operator who wants one member quiet now: it
        writes an explicit cut against every member present, which is a
        *snapshot* — members who join later are wired by their own join like
        anyone else. Starting a spawned child off connected to its parent
        alone was once this method's job and is no longer: that is the join's
        wiring (``MeshManager._wire_member``), which needs no cuts at all.
        """
        kept = set(keep) | {handle}
        cut = []
        for other in sorted(self.members):
            if other in kept:
                continue
            self.member_edges[self.member_key(handle, other)] = False
            cut.append(other)
        return cut

    def prune_member_edges(self) -> None:
        """Drop edges naming a member that has left, so a rejoining handle
        does not silently inherit the cuts made against its predecessor."""
        for key in list(self.member_edges):
            if any(h not in self.members for h in key.split("|")):
                del self.member_edges[key]

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    def addressed_to(self, msg: dict, handle: str) -> bool:
        """Is ``handle`` a recipient of ``msg``?

        The member graph is applied here as well as at send time, and it has
        to be: the log stores the *address* (``"*"``, or a handle list), not
        the recipients it resolved to, and delivery, ``pending`` and ``owed``
        all re-derive membership from that address. Checking only on the way
        in would leave a broadcast reaching everyone on the way out — an ACL
        with a second entrance is not an ACL.

        Re-derivation makes this current rather than historical: a message
        already accepted stops being delivered if the edge is cut before it
        lands. That is the same direction the fast path takes when it
        re-resolves, and the safe one — the alternative delivers across a
        connection the operator has just removed.
        """
        sender = msg.get("from")
        to = msg.get("to")
        if to == "*":
            addressed = sender != handle
        elif isinstance(to, list):
            addressed = handle in to
        else:
            addressed = to == handle
        # An external send (an operator speaking as themselves) has no member
        # behind it, so it is in no one's graph and `connected` waves it
        # through on the unknown-pair default. That is intended: the human is
        # not a member and is not subject to the members' topology.
        return addressed and self.connected(str(sender or ""), handle)

    def pending(self, handle: str) -> List[dict]:
        """Undelivered messages for ``handle`` — sequenced tail first, then
        anything the fast path parked in ``provisional``."""
        start = self.cursors.get(handle, 0)
        done = self.delivered_ids.get(handle) or frozenset()
        log = self.messages
        out = [
            m for m in (log[k] for k in self._indexed().candidates(handle, start))
            if self.addressed_to(m, handle) and m.get("id") not in done
        ]
        out.extend(
            m for m in self.provisional
            if self.addressed_to(m, handle) and m.get("id") not in done
        )
        return out

    def delivery_of(self, msg: dict, index: Optional[int] = None) -> dict:
        """Where this message actually got to: who it resolves to now, and
        which of them have already had it typed in.

        The log stores the *address* rather than the recipients it resolved to
        (see :meth:`addressed_to`), so a reader of the history cannot tell who
        a ``"*"`` reached, nor that a since-cut edge has taken someone off it.
        This answers both, by the same re-derivation delivery itself uses — so
        a picture drawn from it cannot disagree with what the daemon is doing.

        ``delivered`` is claimed only for LOCAL members, because it is only
        known for them: a remote member is injected by its own daemon and
        consumes from its own cursor. Calling that "not delivered" would read
        as a stuck delivery, which is the state this annotation exists to make
        visible — so they are listed apart, under ``remote``, and the reader is
        told the difference instead of being guessed at.

        ``index`` is the message's position in :attr:`messages`; omit it for a
        provisional one (parked by the fast path, not yet sequenced), whose
        only evidence of delivery is ``delivered_ids``.
        """
        recipients = [h for h in self.members if self.addressed_to(msg, h)]
        mid = msg.get("id")
        delivered: List[str] = []
        remote: List[str] = []
        for handle in recipients:
            if not self.members[handle].local:
                remote.append(handle)
                continue
            done = self.delivered_ids.get(handle) or frozenset()
            if mid in done or (
                index is not None and index < self.cursors.get(handle, 0)
            ):
                delivered.append(handle)
        return {"recipients": recipients, "delivered": delivered, "remote": remote}

    def ack_timeout(self) -> dict:
        """This mesh's ack-timeout settings: ``{enabled, owed_secs,
        door_secs}``.

        Read defensively, like :meth:`MeshManager.backpressure` beside it: a
        ``mesh.json`` written before the section existed has no
        ``ack_timeout`` key, and neither a ledger nor a delivery may fall
        over because of that — an absent section means the pre-timeout
        behaviour (nothing ever expires), which is what those files were
        running under anyway.

        The two clocks are separate because the two debts are different. A
        DELIVERED question the member never answered is an obligation, and
        ``owed_secs`` writes it off. An UNDELIVERED message is not the
        member's debt at all — it is the daemon's — and ``door_secs`` only
        stops it counting toward ``backpressure.inbox_max``; the message is
        still queued and still lands on respawn.
        """
        pol = self.policy.get("ack_timeout") or {}
        if not isinstance(pol, dict) or not pol.get("enabled", False):
            return {"enabled": False, "owed_secs": 0.0, "door_secs": 0.0}
        try:
            return {
                "enabled": True,
                "owed_secs": max(0.0, float(pol.get("owed_secs", 0.0) or 0.0)),
                "door_secs": max(0.0, float(pol.get("door_secs", 0.0) or 0.0)),
            }
        except (TypeError, ValueError):
            return {"enabled": False, "owed_secs": 0.0, "door_secs": 0.0}

    def owed(self, handle: str) -> List[dict]:
        """Reply-expecting messages already DELIVERED to ``handle`` that it
        has not answered — the per-message form of the policy engine's
        ``unanswered`` flag, which is only ever a boolean.

        Resolution follows the nudger exactly (``mesh_policy.tick`` compares
        ``last_sent`` against ``last_asked``): ANY message the member sends
        closes everything delivered to it beforehand. So this list can never
        claim a debt the daemon is not also chasing — a dashboard that
        disagreed with the heartbeat would be worse than no dashboard. The
        rule is deliberately forgiving (one reply closes three questions),
        and that is what makes the remainder worth reading: what survives is
        mail nobody has said anything about at all.

        Undelivered mail is NOT owed — see :meth:`pending`. The member has
        not seen it yet, so that debt is the daemon's, not the member's, and
        the two are diagnosed differently (a stuck delivery vs. a silent
        agent). ``fyi``/``ack``/``ping`` never count: they were sent
        precisely to say nothing is owed.

        Messages the operator has **dismissed** are gone from here too — the
        one closure that is not a reply. It is a deliberate second door: some
        mail is never going to be answered (the asker moved on, the member
        was restarted mid-question, the question answered itself), and the
        list is only worth reading if what stays on it is what still matters.
        """
        dropped = self.dismissed.get(handle) or frozenset()
        secs = self.ack_timeout()["owed_secs"]
        now = datetime.now(timezone.utc) if secs else None
        out: List[dict] = []
        for m in self.owed_all(handle):
            if m.get("id") in dropped:
                continue
            if now is not None:
                age = _age_secs(m.get("ts"), now)
                # An unparsable/absent ts is NOT expired: the timeout may
                # only ever forgive a debt it can actually date.
                if age is not None and age >= secs:
                    continue
            out.append(m)
        return out

    def owed_all(self, handle: str) -> List[dict]:
        """:meth:`owed` before the operator's dismissals are subtracted.

        Kept apart so a dismissal set can be pruned against the same window
        the ledger is derived from: an id that has fallen out of it (the
        member finally spoke, the edge was cut, the message aged past its own
        reply) is not being suppressed by anything and should not be
        remembered as if it were.
        """
        start = self.cursors.get(handle, 0)
        done = self.delivered_ids.get(handle) or frozenset()
        out: List[dict] = []
        # A joining member's cursor jumps to the end of the log (see
        # ``MeshManager.join``), which makes every earlier message read as
        # "already delivered" to it. Without a floor the walk below then
        # charges a member that has never spoken with the whole history it
        # arrived after — mail sent before it existed, addressed to a '*'
        # that did not include it. Its join is that floor.
        member = self.members.get(handle)
        # ONE clock for both sides of the comparison below. Read twice, the
        # join is dated against an earlier "now" than the messages are, so a
        # message sent in the same instant measures fractionally OLDER than
        # the join and trips a floor meant only for real history.
        now_wall = datetime.now(timezone.utc)
        joined_ago = (
            _age_secs(member.joined_at, now_wall)
            if member is not None and member.joined_at
            else None
        )
        # Backwards from the newest, stopping at this member's own last send —
        # the unsequenced tail first, then the log. mesh_info calls this for
        # every member on every roster read, so the log half visits only the
        # positions that can be addressed to this member (see _LogIndex):
        # an entry sent to someone else can be neither owed nor, by the
        # message order the join floor relies on, the first to predate it.
        index = self._indexed()
        walk = [(m, m.get("id") in done) for m in reversed(self.provisional)]
        stop = False
        for m, delivered in walk:
            if m.get("from") == handle:
                stop = True
                break
            if joined_ago is not None:
                age = _age_secs(m.get("ts"), now_wall)
                if age is not None and age > joined_ago:
                    stop = True
                    break  # predates this member's join — never its debt
            if delivered and self.addressed_to(m, handle) and expects_reply(
                msg_type_for(m, handle)
            ):
                out.append(m)
        if not stop:
            floor = index.last_from.get(handle, -1) + 1
            for k in reversed(index.candidates(handle, floor)):
                m = self.messages[k]
                if joined_ago is not None:
                    age = _age_secs(m.get("ts"), now_wall)
                    if age is not None and age > joined_ago:
                        break  # predates this member's join — never its debt
                if not (k < start or m.get("id") in done):
                    continue
                if not self.addressed_to(m, handle):
                    continue
                if expects_reply(msg_type_for(m, handle)):
                    out.append(m)
        out.reverse()
        return out

    def _indexed(self) -> _LogIndex:
        """The address index, caught up with the log (see :class:`_LogIndex`)."""
        index = self._log_index
        log = self.messages
        if (
            index.log is not log
            or index.size != len(log)
            or (log and log[-1] is not index.last)
        ):
            index.sync(log)
        return index


class MeshManager:
    """Registry of meshes plus their delivery workers. Event-loop only."""

    def __init__(
        self,
        manager: SessionManager,
        *,
        settle: float = DEFAULT_SETTLE,
        busy_hold: float = DEFAULT_BUSY_HOLD,
        root: Optional[Path] = None,
    ) -> None:
        self.manager = manager
        self.settle = settle
        self.busy_hold = busy_hold
        # Storage root override (tests run several daemons in one process);
        # None = the daemon's global mesh directory.
        self._root = root
        self._meshes: Dict[str, Mesh] = {}
        #: urgent-send rate limits (monotonic stamps); a daemon restart resets them.
        self._urgent_sent: Dict[str, List[float]] = {}
        self._urgent_pair: Dict[Tuple[str, str], float] = {}
        self._workers: Dict[str, asyncio.Task] = {}
        #: Sessions whose join briefing the onboarding path is folding into a
        #: single opening block. Held only for the length of one join call.
        self._brief_deferred: set = set()
        self._started = False
        #: Federation wiring, set by the daemon entrypoint once the uplink
        #: exists. ``machine`` is this daemon's relay name — the machine-level
        #: qualifier in global addresses like ``work-pc/s0``; empty means no
        #: relay configured, so the mesh is local-only. Assigning it refreshes
        #: every mesh's view of its own rank (see the property below).
        self._machine: str = ""
        #: async (machine, path, body) -> dict; raises PeerUnreachable on
        #: transport failure, MeshError on an application-level rejection.
        self.peer_transport: Optional[Callable] = None
        #: async (machine, path, body) -> PeerBridge: the same authenticated
        #: request with a response read live (the shadow terminal,
        #: daemon/shadow.py); raises PeerUnreachable like peer_transport.
        self.peer_streamer: Optional[Callable] = None
        self.relay_connected: Callable[[], bool] = lambda: False
        #: async () -> [machine names] — the other backends on our relay
        #: (RelayUplink.peer_list); None when no uplink or an old relay.
        self.peer_lister: Optional[Callable] = None
        #: async (sender session, board status) -> [session names]: the
        #: assignees of the issues in that status on the board the sender's
        #: directory files on. Set by the daemon entrypoint, which owns the
        #: board; None means ``to`` selectors are refused (see
        #: :data:`AUDIENCE_SELECTORS`).
        self.audience_resolver: Optional[Callable] = None
        #: How often (seconds) a policy-enabled primary syncs each guest even
        #: with nothing to send, so activity reports stay fresh.
        self.report_interval: float = 10.0
        #: Invite tickets expire after this many seconds (pre-approval only;
        #: see "Membership-first joining" in docs/mesh-design.md).
        self.invite_ttl: float = 86400.0
        #: Our own outbound join requests awaiting the primary's decision,
        #: request_id -> {request_id, mesh, primary, reply_token, session,
        #: handle, role, requested_at}. Durable (outgoing_joins.json) so a
        #: grant that arrives after a restart still finds its request.
        self._outgoing: Dict[str, dict] = {}
        #: Meshes other daemons offered to this one (``invited`` visibility),
        #: "mesh@machine" -> {mesh, machine, token, project, offered_at}.
        #: Durable (mesh_offers.json): an offer is pushed once, and a daemon
        #: that restarts must still list it.
        self._offers: Dict[str, dict] = {}
        #: Local key -> directory, for a mesh whose directory is not its key
        #: (see _mesh_dir).
        self._dirs: Dict[str, Path] = {}

    @property
    def machine(self) -> str:
        return self._machine

    @machine.setter
    def machine(self, value: str) -> None:
        """Our relay name. The uplink resolves it *after* ``load_all``, so
        every mesh's ``me`` (and with it its rank) is refreshed here."""
        self._machine = str(value or "")
        if self._machine == LOCAL_HOST:
            log.warning(
                "relay name %r is reserved for addresses (mesh@local means "
                "this daemon) — peers cannot address this daemon's meshes",
                LOCAL_HOST,
            )
        for mesh in self._meshes.values():
            # An empty relay name never *erases* a remembered identity: a
            # federated mesh keeps the rank it was written with until the
            # uplink offers a real name (possibly a renamed one).
            if self._machine:
                if mesh.me and mesh.me != self._machine and mesh.peers:
                    try:
                        self._renamed_self(mesh, mesh.me, self._machine)
                    except MeshError as exc:
                        log.error(
                            "mesh %r: cannot take the new relay name %r: %s",
                            mesh.name, self._machine, exc,
                        )
                mesh.me = self._machine
            self._migrate_v2(mesh)

    def _is_local(self, mesh: Mesh, member: Member) -> bool:
        """Whether ``member``'s session lives on THIS daemon.

        The roster is absolute (v2): the primary's own members carry
        ``machine == ""``, guest members carry their guest's machine name. On
        a mirror, only members stamped with our machine are ours.
        """
        if mesh.primary:
            return bool(self.machine) and member.machine == self.machine
        return member.machine in ("", self.machine)

    #: Same question, asked from outside. The rule above is subtle enough
    #: (blank machine means two different things either side of `primary`)
    #: that a caller reimplementing it would get it wrong on a mirror.
    is_local_member = _is_local

    def machine_name(self, mesh: Mesh, member: Member) -> str:
        """The daemon name to PRINT for ``member`` — "" when it is this one.

        ``Member.machine`` is not readable on its own for this: a blank means
        "the primary's own" on a mirror and "not stamped yet" on the
        authority, so the question has to go through :meth:`_is_local` first.
        """
        if self._is_local(mesh, member):
            return self.machine or ""
        return member.machine or ""

    def meshes_for_session(self, session: str) -> List[dict]:
        """Every mesh THIS daemon's ``session`` is a member of, as
        ``{mesh, handle, role, joined_at, members}``.

        The roster is keyed by handle, and a handle on a mirrored mesh may
        name a session on another machine — so locality is decided by
        :meth:`_is_local`, not by the session name alone.
        """
        out: List[dict] = []
        for mesh in self.list():
            for handle in sorted(mesh.members):
                member = mesh.members[handle]
                if member.session == session and self._is_local(mesh, member):
                    out.append(
                        {
                            "mesh": mesh.name,
                            "handle": member.handle,
                            "role": member.role,
                            "subroles": list(member.subroles),
                            "roles": member.roles,
                            "joined_at": member.joined_at,
                            "members": len(mesh.members),
                        }
                    )
        return out

    # ------------------------------------------------------------------ #
    # backpressure
    # ------------------------------------------------------------------ #
    def backpressure(self, mesh: Mesh) -> dict:
        """This mesh's backpressure settings: ``{enabled, inbox_max,
        min_gap, retry_after}``.

        Read defensively rather than indexed: a ``mesh.json`` written before
        the section existed has no ``backpressure`` key, and a delivery
        worker must not raise over a config that is merely old.
        """
        pol = mesh.policy.get("backpressure") or {}
        try:
            return {
                "enabled": bool(pol.get("enabled", False)),
                "inbox_max": max(0, int(pol.get("inbox_max", 0) or 0)),
                "min_gap": max(0.0, float(pol.get("min_gap", 0.0) or 0.0)),
                "retry_after": max(0.0, float(pol.get("retry_after", 0.0) or 0.0)),
            }
        except (TypeError, ValueError):
            return {
                "enabled": False, "inbox_max": 0,
                "min_gap": 0.0, "retry_after": 0.0,
            }

    def inbox_depth(self, mesh: Mesh, handle: str) -> Optional[int]:
        """Undelivered messages waiting for ``handle`` — or None if this
        daemon cannot know.

        For a LOCAL member it is the same list delivery is about to type in
        (:meth:`Mesh.pending`), so the door and the worker agree by
        construction. For a member hosted elsewhere the cursor lives on that
        daemon and the freshest reading we have is the one its sync ack
        piggybacked (``remote_activity``) — lagged by up to a sync interval,
        which makes the cap soft there rather than exact. None (a member we
        have never had a report for) is NOT congestion: refusing on an
        absence of evidence would cut a peer off for being new.
        """
        member = mesh.members.get(handle)
        if member is None:
            return None
        if self._is_local(mesh, member):
            return len(mesh.pending(handle))
        depth = (mesh.remote_activity.get(handle) or {}).get("pending")
        if isinstance(depth, bool) or not isinstance(depth, (int, float)):
            return None
        return max(0, int(depth))

    def countable_inbox(self, mesh: Mesh, handle: str) -> Optional[int]:
        """:meth:`inbox_depth` minus mail that has aged past
        ``ack_timeout.door_secs`` — what the DOOR actually weighs.

        The cap exists to stop a fan-in arriving faster than a terminal can
        read. That is a statement about RECENT pressure, but the depth it
        was measured against is a total, and the two only agree while the
        queue is draining. When it is not draining — the member's session
        exited, so delivery holds its cursor and returns (see the delivery
        worker) — the total never falls again, and the door that was meant
        to pace a burst becomes a wall nothing can take down. A leader then
        cannot address that handle for the rest of the mesh's life, and the
        bounce it reads ("wait about 90s and re-send") is advice that will
        never once come true.

        So mail past ``door_secs`` stops being weighed. It is NOT dropped —
        it stays queued and still lands if the session is respawned, which
        is a contract of its own — it just stops holding the door shut
        against everybody who came later.

        What that leaves is a leaky bucket, and for a terminal that is
        merely slow the leak is the point: the queue drains, so the aging
        only forgives pressure that is already gone. For a terminal that is
        not reading at all it was a bucket with no bottom — ``inbox_max``
        more messages per ``door_secs``, without end, because nothing
        consumed them. So :meth:`congested_recipients` weighs those
        receivers on the true depth instead, and the aging here governs
        LIVE receivers alone: the ones whose queue it was ever about.

        Remote members are returned unaged: their queue lives on their own
        daemon and all we hold is a depth it piggybacked on a sync ack, with
        no per-message timestamps to age. That daemon applies its own door
        to its own members, which is where the per-message evidence is.
        """
        depth = self.inbox_depth(mesh, handle)
        if depth is None:
            return None
        secs = mesh.ack_timeout()["door_secs"]
        if not secs:
            return depth
        member = mesh.members.get(handle)
        if member is not None and not self._is_local(mesh, member):
            return depth
        now = datetime.now(timezone.utc)
        fresh = 0
        for msg in mesh.pending(handle):
            age = _age_secs(msg.get("ts"), now)
            # Undatable mail is counted, for the same reason the ledger
            # refuses to expire it: the clock may only forgive what it can date.
            if age is None or age < secs:
                fresh += 1
        return fresh

    def receiver_delivery_held(self, mesh: Mesh, handle: str) -> bool:
        """Whether ``handle``'s receiver has explicitly held delivery.

        Local state is authoritative.  For a remote receiver, the primary
        uses the latest activity report, with the same sync-delay limitation
        as :meth:`inbox_depth`.
        """
        member = mesh.members.get(handle)
        if member is None:
            return False
        if not self._is_local(mesh, member):
            return bool(
                (mesh.remote_activity.get(handle) or {}).get("delivery_hold")
            )
        try:
            session = self.manager.get(member.session)
        except ManagerError:
            return False
        held = getattr(session, "delivery_held", None)
        return bool(
            not getattr(session, "exited", False)
            and callable(held)
            and held()
        )

    def congested_recipients(
        self, mesh: Mesh, recipients: Iterable[str]
    ) -> List[dict]:
        """Which of ``recipients`` are too far behind to accept another
        message, as ``{handle, queued, inbox_max, retry_after, remote}``.

        One cap — ``backpressure.inbox_max`` — weighed against one of two
        depths, and the receiver's state picks which. A receiver that is
        reading is weighed on recent traffic (:meth:`countable_inbox`); a
        receiver whose queue cannot drain at all is weighed on the true
        depth, so mail that has aged out of the traffic window still counts
        against it. The second group is an explicit delivery hold, and a
        session that has exited or left the registry — the states
        :meth:`stranded_recipients` reports, which are the states
        :meth:`_deliver_to` refuses to deliver into.

        The refusal is not a loss of the message's purpose: ``_send_core``
        records it on the sender's own loop ledger, where a re-briefing
        hands it back. What it costs is the queue-until-respawn contract
        past the cap, and what it buys is a sender that is told so while it
        can still act.

        ``reason`` names which wall was hit, because the remedies differ:
        ``delivery_hold`` (a person resumes delivery), ``exited`` (respawn
        the session) and ``missing`` (nothing can revive it). All three
        carry ``retry_after: 0.0`` — waiting alone never opens them, and a
        sender told to wait 90s for a door that time does not open spends
        every later turn on the same bounce.
        """
        bp = self.backpressure(mesh)
        # ``inbox_max: 0`` and ``enabled: false`` are documented as "no
        # door, unbounded queueing again" (mesh_policy.default_policy), so
        # there is nothing to weigh against.
        if not (bp["enabled"] and bp["inbox_max"]):
            return []
        # Materialised because it is walked twice, and the parameter is an
        # Iterable: a generator would be empty by the time the loop runs.
        recipients = list(recipients)
        # One pass for the whole batch: the call walks every recipient, so
        # asking it per handle inside the loop would make this quadratic.
        stranded = {
            e["handle"]: str(e.get("state") or "exited")
            for e in self.stranded_recipients(mesh, recipients)
        }
        out: List[dict] = []
        for handle in recipients:
            depth = self.inbox_depth(mesh, handle)
            reason = (
                "delivery_hold"
                if self.receiver_delivery_held(mesh, handle)
                else stranded.get(handle)
            )
            # WHICH depth the door weighs is the whole of it, and the
            # receiver's state picks it.
            #
            # For a receiver that is reading, aging past ``door_secs`` is
            # right: its queue drains, so old mail is pressure that has
            # already gone, and weighing it would shut the door over a burst
            # that is over. Weighed on the countable depth, REPORTED on the
            # true one — the sender is refused for recent pressure, but what
            # is waiting for that terminal is the number it needs to see.
            #
            # For a receiver that is not reading at all — an explicit
            # delivery hold, or a session that exited — nothing consumes the
            # queue, so nothing about it is stale and the aging had no
            # meaning to supply. It only reopened the door once per
            # ``door_secs``, forever, and the backlog grew without a
            # ceiling: the 45-deep queue that led here, which a respawn
            # would have typed into that terminal in ONE block. Those
            # receivers are weighed on the true depth, so the cap the
            # operator set is the cap that holds.
            weighed = depth if reason else self.countable_inbox(mesh, handle)
            if weighed is None or weighed < bp["inbox_max"]:
                continue
            member = mesh.members.get(handle)
            entry = {
                "handle": handle,
                "queued": depth if depth is not None else weighed,
                "inbox_max": bp["inbox_max"],
                "retry_after": 0.0 if reason else bp["retry_after"],
                "remote": bool(
                    member is not None and not self._is_local(mesh, member)
                ),
            }
            if reason:
                entry["reason"] = reason
            out.append(entry)
        return out

    def _record_refusal(self, mesh: Mesh, handle: str, sender: str) -> None:
        """Remember that ``sender`` was turned away from ``handle``.

        The refusal is the only trace a bounce leaves on this side — the
        message was never appended, so the log cannot show it, and the
        sender's own terminal is the only other place it is written down.
        Without this the dashboard would show a terminal with an empty
        backlog and no way to tell that the emptiness IS the mesh holding
        the door shut.
        """
        st = mesh.activity.setdefault(handle, {"anchor": time.monotonic()})
        rec = st.setdefault("refused", [])
        rec.append({"at": time.monotonic(), "from": sender})
        del rec[:-_REFUSED_KEEP]

    def refusals(self, mesh: Mesh, handle: str) -> List[dict]:
        """Recent refusals against ``handle``, oldest first, window-trimmed.

        Trimmed on read rather than on a timer: nothing else wakes for a
        refusal, and a list nobody is looking at does not need to be tidy.
        """
        st = mesh.activity.get(handle) or {}
        rec = st.get("refused") or []
        now = time.monotonic()
        keep = [r for r in rec if now - r.get("at", 0.0) <= _REFUSED_WINDOW]
        if len(keep) != len(rec):
            if keep:
                st["refused"] = keep
            else:
                st.pop("refused", None)
        return keep

    def paced_for(self, mesh: Mesh, handle: str) -> float:
        """Seconds left on ``handle``'s delivery pacing gate (0.0 = none).

        The read half of the gate in :meth:`_deliver_to`, so the chip that
        says "paced" and the worker that is pacing cannot disagree.
        """
        bp = self.backpressure(mesh)
        if not bp["enabled"] or not bp["min_gap"]:
            return 0.0
        last = (mesh.activity.get(handle) or {}).get("last_delivered")
        if last is None:
            return 0.0
        return max(0.0, bp["min_gap"] - (time.monotonic() - last))

    def backpressure_for_session(self, session: str) -> dict:
        """What backpressure is doing to ``session`` right now, across every
        mesh it is a member of.

        Aggregated at the top (``queued``/``congested``/``refused``/
        ``paced_for``) because that is the question the header chip asks —
        "is anything being turned away from this terminal, and is delivery
        into it being paced" — and broken out per handle below, because a
        session in two meshes can be congested in one and quiet in the
        other, and the two meshes may be configured differently.
        """
        handles: List[dict] = []
        enabled = False
        queued = refused = 0
        congested = False
        paced = 0.0
        senders: Dict[str, dict] = {}
        now = time.monotonic()
        for mesh in self.list():
            bp = self.backpressure(mesh)
            for handle in sorted(mesh.members):
                member = mesh.members[handle]
                if member.session != session or not self._is_local(mesh, member):
                    continue
                depth = len(mesh.pending(handle))
                gate = self.paced_for(mesh, handle)
                recs = self.refusals(mesh, handle)
                # Asked of the door itself rather than recomputed here. This
                # was a second copy of the rule, comparing the full depth
                # against the cap while the door weighed the aged one, so the
                # two disagreed for exactly the backlogs a reader looks at:
                # the 45-deep queue that led to this change was reported
                # "congested, cap 4" by this call while the door was in fact
                # letting four more in per ``door_secs``.
                hot = bool(self.congested_recipients(mesh, [handle]))
                enabled = enabled or bp["enabled"]
                queued += depth
                refused += len(recs)
                congested = congested or hot
                paced = max(paced, gate)
                for r in recs:
                    who = str(r.get("from") or "?")
                    ent = senders.setdefault(
                        who, {"from": who, "count": 0, "ago": None}
                    )
                    ent["count"] += 1
                    ago = max(0.0, now - r.get("at", now))
                    if ent["ago"] is None or ago < ent["ago"]:
                        ent["ago"] = ago
                handles.append(
                    {
                        "mesh": mesh.name,
                        "handle": handle,
                        "queued": depth,
                        "congested": hot,
                        "paced_for": gate or None,
                        "refused": len(recs),
                        **bp,
                    }
                )
        return {
            "enabled": enabled,
            "queued": queued,
            "congested": congested,
            # The cap that is actually biting, so a chip can say "4/4"
            # without walking ``handles`` itself: the congested handle's own
            # cap when one is congested (that is the number being hit), the
            # loosest configured cap otherwise (nothing is being hit, and
            # the roomiest room is the honest headline).
            "inbox_max": max(
                [h["inbox_max"] for h in handles if h["congested"]]
                or [h["inbox_max"] for h in handles]
                or [0]
            ),
            "paced_for": paced or None,
            "refused": refused,
            # Who is being turned away, worst offender first — the answer to
            # "who is flooding this session", which is why a person opens
            # this panel at all.
            "refused_from": sorted(
                senders.values(), key=lambda e: (-e["count"], e["from"])
            ),
            "handles": handles,
        }

    def queued_for_session(self, session: str) -> List[dict]:
        """Messages accepted for ``session``'s handles but not yet typed into
        its terminal — the delivery worker's backlog, re-derived exactly the
        way :meth:`_deliver_to` derives it (:meth:`Mesh.pending`), so this
        view cannot disagree with what the worker is about to type in.

        Each entry carries the recipient's OWN slice of the body (see
        :func:`recipient_body`), clipped the way delivery clips it, and how
        long that handle's backlog has been waiting (``held_for``, seconds).
        Ordered oldest first — the order delivery will type them.
        """
        out: List[dict] = []
        now = time.monotonic()
        for mesh in self.list():
            for handle in sorted(mesh.members):
                member = mesh.members[handle]
                if member.session != session or not self._is_local(mesh, member):
                    continue
                first = mesh._first_pending.get(handle)
                for m in mesh.pending(handle):
                    body = recipient_body(m, handle)
                    if len(body) > MAX_DELIVERY_BODY:
                        body = body[:MAX_DELIVERY_BODY] + " …[clipped]"
                    out.append(
                        {
                            "mesh": mesh.name,
                            "handle": handle,
                            "id": m.get("id"),
                            "ts": m.get("ts"),
                            "from": m.get("from"),
                            "type": msg_type_for(m, handle),
                            "reply_to": m.get("reply_to"),
                            "body": body,
                            "held_for": (now - first) if first is not None else None,
                        }
                    )
        out.sort(key=lambda e: str(e.get("ts") or ""))
        return out

    async def flush_session(self, session: str) -> dict:
        """Type ``session``'s held backlog in now, at a human's say-so.

        The write half of :meth:`queued_for_session`, and the answer to the
        one thing the queued banner could previously only describe: a message
        is sitting there because the agent is mid-turn (or a keyboard is
        live), and the operator can see it is not going to matter — but the
        daemon still waits out ``busy_hold`` because it cannot know that.

        What this drops is every hold a person is in a position to overrule:
        the idle-gate in :meth:`_deliver_to`, the pinned hold ahead of it,
        and — through ``force`` — the keyboard holds inside
        :meth:`Session.deliver`, where the wait shortens and an unsent line
        is submitted ahead of the delivery instead of refusing it. Nobody's
        line is eaten: it goes to the agent first, as its own message.

        The one hold that stays is :meth:`Session._await_readable`. A TUI
        that has not mounted its input yet is not somebody holding the
        message back, and typing into it does not deliver the message
        sooner, it delivers a broken one — typed into nothing, or typed and
        never submitted. That wait is bounded and ends by itself.

        Returns ``{"flushed": n, "handles": [...]}`` — how many messages went
        in and to which handles, so the caller can say what happened rather
        than assert that something did. An exited or busy-forever session
        simply flushes nothing, which is the honest answer.
        """
        flushed = 0
        handles: List[str] = []
        for mesh in self.list():
            for handle in sorted(mesh.members):
                member = mesh.members[handle]
                if member.session != session or not self._is_local(mesh, member):
                    continue
                waiting = len(mesh.pending(handle))
                if not waiting:
                    continue
                await self._deliver_to(mesh, member, force=True)
                # Delivery is best-effort: it advances the cursor only when
                # the paste landed, so re-reading the backlog is what tells
                # us whether it did — never the fact that we asked.
                went = waiting - len(mesh.pending(handle))
                if went > 0:
                    flushed += went
                    handles.append(f"{handle}@{mesh.name}")
        return {"flushed": flushed, "handles": handles}

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def _mesh_root(self) -> Path:
        return self._root if self._root is not None else paths.mesh_root()

    def _mesh_dir(self, name: str) -> Path:
        """Where mesh ``name`` (its local key) lives on disk. Normally the
        key itself; a mirror whose directory could not be moved to its
        address key keeps the directory it was loaded from."""
        pinned = self._dirs.get(name)
        return pinned if pinned is not None else self._mesh_root() / name

    def load_all(self) -> None:
        root = self._mesh_root()
        if not root.is_dir():
            return
        for entry in sorted(root.iterdir()):
            if not (entry / "mesh.json").is_file():
                continue
            if _RETIRED_RE.search(entry.name):
                continue  # a deleted mesh's history, kept but not mounted
            try:
                mesh = self._load(entry)
            except (OSError, ValueError, KeyError) as exc:
                log.warning("skipping unreadable mesh %r: %s", entry.name, exc)
                continue
            # Key by the mesh's OWN name, never the directory's. They agree
            # for a live mesh, and when they do not the mesh is reachable in
            # the listing but not by name — every lookup answers "no mesh
            # named ...", including the delete that would clear it.
            if mesh.name in self._meshes:
                log.warning(
                    "mesh %r: %r also claims that name — ignoring the second",
                    mesh.name, str(entry),
                )
                continue
            self._meshes[mesh.name] = mesh
            if entry.name != mesh.name:
                # A mirror written before addresses lives under its bare
                # name; its key is now name@origin. Move it so a local mesh
                # of the same name can be created beside it.
                target = self._mesh_dir(mesh.name)
                try:
                    if target.exists():
                        raise OSError(f"{target} already exists")
                    entry.rename(target)
                    log.info("mesh %r: moved %s -> %s", mesh.name, entry.name,
                             target.name)
                except OSError as exc:
                    log.warning("mesh %r: keeping %s (%s)", mesh.name, entry, exc)
                    self._dirs[mesh.name] = entry
            if getattr(mesh, "_origin_migrated", False):
                self._persist_def(mesh)
        outgoing_path = root / "outgoing_joins.json"
        if outgoing_path.is_file():
            try:
                records = json.loads(outgoing_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                records = []
            for rec in records if isinstance(records, list) else []:
                if isinstance(rec, dict) and rec.get("request_id"):
                    self._outgoing[str(rec["request_id"])] = rec
        offers_path = root / "mesh_offers.json"
        if offers_path.is_file():
            try:
                offers = json.loads(offers_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                offers = []
            for rec in offers if isinstance(offers, list) else []:
                if isinstance(rec, dict) and rec.get("mesh") and rec.get("machine"):
                    self._offers[f"{rec['mesh']}@{rec['machine']}"] = rec

    def start(self) -> None:
        """Spawn delivery workers (requires a running event loop)."""
        self._started = True
        for name in self._meshes:
            self._ensure_worker(name)

    async def shutdown(self) -> None:
        for task in self._workers.values():
            task.cancel()
        for task in self._workers.values():
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self._workers.clear()

    # ------------------------------------------------------------------ #
    # registry operations
    # ------------------------------------------------------------------ #
    def create(self, name: str, *, project: str = "") -> Mesh:
        """Create a mesh, filed under ``project`` (blank = the default).

        An unknown project is refused rather than recorded: a mesh filed
        under a name the registry does not know would be reachable from no
        project's listing, which is the one failure the tier exists to
        prevent.
        """
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            raise MeshError(
                f"invalid mesh name {name!r}: use letters, digits, '.', '_' or '-'"
            )
        if name in self._meshes:
            raise MeshConflict(f"mesh {name!r} already exists")
        try:
            project = projects.require(project).name
        except projects.ProjectError as exc:
            raise MeshError(str(exc)) from None
        mesh = Mesh(name, me=self.machine, project=project)
        self._meshes[name] = mesh
        self._persist_def(mesh)
        self._ensure_worker(name)
        return mesh

    def get(self, name: str) -> Mesh:
        """Resolve a mesh reference (docs/mesh-design.md "Mesh addresses").

        ``dev@pca`` is the mesh ``dev`` created on ``pca``; ``dev@local`` (or
        ``dev@<this daemon>``) is ours. The ``@local`` may be left off, which
        is every reference written before addresses existed: a bare ``dev``
        is ours if we have one, else the one mirror named ``dev`` — and an
        error naming the candidates when there are several, rather than a
        guess.
        """
        ref = (name or "").strip()
        hit = self._find(ref)
        if hit is None:
            raise MeshError(f"no mesh named {ref!r}")
        return hit

    def address(self, mesh: Mesh) -> str:
        """The mesh's global address, ``name@creator`` — ours spelled with
        our relay name (``@local`` before we have one)."""
        return f"{mesh.wire_name}@{mesh.origin or self.machine or LOCAL_HOST}"

    def _is_me(self, host: str) -> bool:
        return host == LOCAL_HOST or (bool(self.machine) and host == self.machine)

    def _find(self, ref: str) -> Optional[Mesh]:
        """:meth:`get` without the not-found error (an ambiguous bare name
        still raises: answering None there would read as "absent")."""
        hit = self._meshes.get(ref)
        if hit is not None:
            return hit
        base, sep, host = ref.partition("@")
        if sep:
            if self._is_me(host):
                hit = self._meshes.get(base)
                return hit if hit is not None and not hit.origin else None
            # Addressed by the daemon holding authority now rather than the
            # one that created it (phase 7 moves authority).
            moved = [
                m for m in self._meshes.values()
                if m.wire_name == base and m.origin and host in m.peers
            ]
            return moved[0] if len(moved) == 1 else None
        same = [m for m in self._meshes.values() if m.wire_name == ref]
        if len(same) == 1:
            return same[0]
        if same:
            raise MeshError(
                f"{len(same)} meshes are named {ref!r} here — say which: "
                + ", ".join(sorted(m.name for m in same))
            )
        return None

    def _inbound(
        self, name: str, machine: str, token: str = "", *,
        authority: bool = False,
    ) -> Mesh:
        """The local mesh a peer call names by its wire name.

        Peers speak bare names (the protocol predates addresses), so with a
        local ``dev`` and a mirror ``dev@pca`` the caller picks by the link
        it authenticates on: the mesh whose link to ``machine`` expects
        ``token``. The token check that follows in every handler is what
        admits the call; this only chooses which mesh to check it against.
        ``authority`` prefers a mesh we are the authority of (a join request
        from a daemon not linked yet).
        """
        cands = [m for m in self._meshes.values() if m.wire_name == name]
        if not cands:
            raise MeshError(f"no mesh named {name!r}")
        if len(cands) == 1:
            return cands[0]
        if token:
            for m in cands:
                link = m.links.get(machine)
                if link and secrets.compare_digest(
                    str(token).encode("utf-8"),
                    str(link.get("token_in") or "").encode("utf-8"),
                ):
                    return m
        linked = [m for m in cands if machine in m.links]
        if len(linked) == 1:
            return linked[0]
        if authority:
            for m in cands:
                if not m.primary and not m.origin:
                    return m
            for m in cands:
                if not m.primary:
                    return m
        for m in cands:
            if m.origin == machine:
                return m
        for m in cands:
            if not m.origin:
                return m
        return cands[0]

    def list(self) -> List[Mesh]:
        return [self._meshes[k] for k in sorted(self._meshes)]

    def delete(self, name: str) -> None:
        mesh = self.get(name)
        # A primary tells its guests so their mirrors are dropped, not
        # orphaned (best-effort — an unreachable guest keeps a dead mirror
        # it can remove locally).
        if not mesh.primary and mesh.links:
            self._notify_unlink_soon(
                mesh.wire_name,
                {m: str(g.get("token_out") or "") for m, g in mesh.links.items()},
            )
        self._drop_mesh(mesh.name)

    def _drop_mesh(self, name: str) -> None:
        mesh = self.get(name)
        task = self._workers.pop(mesh.name, None)
        if task is not None:
            task.cancel()
        del self._meshes[mesh.name]
        # Retire the directory rather than deleting history: rename with a
        # timestamp suffix so a recreated mesh starts clean.
        d = self._mesh_dir(mesh.name)
        self._dirs.pop(mesh.name, None)
        if d.is_dir():
            stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
            try:
                d.rename(d.with_name(f"{mesh.name}.deleted-{stamp}"))
            except OSError:
                pass

    def _notify_unlink_soon(self, name: str, tokens: Dict[str, str]) -> None:
        if self.peer_transport is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _notify() -> None:
            for machine, token in tokens.items():
                try:
                    await self.peer_transport(
                        machine,
                        "/peer/mesh/unlink",
                        {"mesh": name, "machine": self.machine, "token": token},
                    )
                except Exception:  # noqa: BLE001 — best-effort only
                    pass

        asyncio.ensure_future(_notify())

    async def join(
        self,
        name: str,
        session: str,
        *,
        handle: str = "",
        role: str = "",
        subroles: Sequence[str] = (),
        code: Optional[str] = None,
    ):
        """Join a local session into a mesh — THE establishment verb.

        ``subroles`` are further roles the member answers for besides
        ``role`` (see :attr:`Member.subroles`); each is resolved through the
        mesh's vocabulary and an unknown one refuses the join like an unknown
        primary role does.

        ``name`` is a mesh name or a global address ``name@machine`` (the
        primary daemon's relay name). For a mesh already known here (owned
        or mirrored) this is a plain member join. For an unknown address it
        *establishes*: with ``code`` (a pre-approval invite ticket) the
        primary grants synchronously — one call creates the mirror, the
        member and the briefing; without a code the request pends on the
        primary for operator approval and a ``{pending, request_id}`` dict
        is returned (the grant arrives later over the relay).
        """
        mesh_name, primary, invite_token = self._parse_addr(name, code)
        # An address names one mesh whichever others share its name: a local
        # ``dev`` and a mirror ``dev@pca`` are two meshes, not a conflict.
        local = self._find(f"{mesh_name}@{primary}" if primary else mesh_name)
        if local is not None:
            return await self._join_local(
                local.name, session, handle=handle, role=role, subroles=subroles
            )
        if not primary or self._is_me(primary):
            raise MeshError(
                f"no mesh named {mesh_name!r} on this daemon — join a remote "
                f"mesh with '{mesh_name}@<machine>', or create it first "
                f"(claunch mesh create {mesh_name})"
            )
        return await self._join_remote(
            mesh_name, primary, invite_token, session,
            handle=handle, role=role, subroles=subroles,
        )

    def _parse_addr(self, name: str, code: Optional[str]):
        """Split ``name[@machine]`` (cross-checked against ``code``'s
        embedded address) -> (mesh_name, primary_machine, invite_token)."""
        name = (name or "").strip()
        mesh_name, _, primary = name.partition("@")
        invite_token = ""
        if code:
            try:
                doc = json.loads(
                    base64.urlsafe_b64decode(code.strip().encode("ascii"))
                )
                code_mesh = str(doc["mesh"])
                code_machine = str(doc["machine"])
                invite_token = str(doc["token"])
            except Exception:
                raise MeshError("invalid invite code") from None
            if mesh_name and mesh_name != code_mesh:
                raise MeshError(
                    f"invite code is for mesh {code_mesh!r}, not {mesh_name!r}"
                )
            if primary and primary != code_machine:
                raise MeshError(
                    f"invite code was minted by {code_machine!r}, not {primary!r}"
                )
            mesh_name = mesh_name or code_mesh
            primary = primary or code_machine
        if not _NAME_RE.match(mesh_name or ""):
            raise MeshError(f"invalid mesh name {mesh_name!r}")
        if primary and not _NAME_RE.match(primary):
            raise MeshError(f"invalid machine name {primary!r}")
        return mesh_name, primary, invite_token

    async def _join_remote(
        self,
        mesh_name: str,
        primary: str,
        invite_token: str,
        session: str,
        *,
        handle: str,
        role: str,
        subroles: Sequence[str] = (),
    ):
        """Establishment join: ask ``primary`` to admit this session.

        The self-declared ``machine`` in the request cannot be verified by
        the callee, but it does not need to be: the grant (with the mesh
        credentials) is delivered via the relay to the *claimed* name, so
        only the daemon actually registered under it can complete the join.
        """
        machine = self._require_machine()
        if primary == machine:
            raise MeshError(
                f"this daemon is {primary!r} but has no mesh named "
                f"{mesh_name!r} — create it first (claunch mesh create "
                f"{mesh_name})"
            )
        if self.peer_transport is None:
            raise MeshError("relay uplink is not running — cannot reach peers")
        try:
            live = self.manager.get(session)
        except ManagerError:
            raise MeshError(
                f"no session named {session!r} on this daemon"
            ) from None
        if live.exited:
            raise MeshError(f"session {session!r} has exited — respawn it first")
        handle = (handle or session).strip()
        if not _NAME_RE.match(handle):
            raise MeshError(
                f"invalid handle {handle!r}: use letters, digits, '.', '_' or '-'"
            )
        # The mirror this join builds is filed where the session is.
        project = self._session_project(session)
        reply_token = secrets.token_urlsafe(18)
        body = {
            "mesh": mesh_name,
            "machine": machine,
            "session": session,
            "handle": handle,
            "role": role,
            "subroles": list(subroles),
            "reply_token": reply_token,
        }
        if invite_token:
            body["code"] = invite_token
        resp = await self.peer_transport(primary, "/peer/mesh/join_request", body)
        if not isinstance(resp, dict):
            raise MeshError("unexpected response from the primary")
        if resp.get("granted"):
            return self._adopt_grant(
                mesh_name, primary, reply_token, resp.get("grant") or {},
                project=project,
            )
        if resp.get("pending"):
            rid = str(resp.get("id") or "")
            rec = {
                "request_id": rid,
                "mesh": mesh_name,
                "primary": primary,
                "reply_token": reply_token,
                "session": session,
                "handle": handle,
                "role": role,
                "subroles": list(subroles),
                "project": project,
                "requested_at": utcnow(),
            }
            self._outgoing[rid] = rec
            self._persist_outgoing()
            return {
                "pending": True,
                "request_id": rid,
                "mesh": mesh_name,
                "primary": primary,
            }
        raise MeshError("unexpected response from the primary")

    def _adopt_grant(
        self, mesh_name: str, primary: str, reply_token: str, grant: dict,
        *, attach: bool = False, project: str = "",
    ) -> Optional[Member]:
        """Build the mirror + our member from a primary's grant payload.

        ``attach`` is the daemon-level join: the grant carries no member of
        ours, so only the mirror is built and None is returned. Sessions here
        then join it like any mesh already present (``_join_local``).

        ``project`` files the mirror locally (the joining session's project,
        or the one the attach named); the owner's filing is its own business.
        A project deleted while the request pended falls back to the default
        rather than refusing a grant the owner already gave.
        """
        # The mesh's address, as its creator named it: a daemon that took
        # authority over from the creator still grants under the creator's
        # name, so every daemon keys the mirror the same way. A granter that
        # predates addresses sends none — it is the creator as far as we know.
        origin = str(grant.get("origin") or primary)
        if self._is_me(origin):
            raise MeshConflict(
                f"mesh {mesh_name!r} was created on this daemon — it cannot "
                "be mirrored here as well"
            )
        existing = self._meshes.get(f"{mesh_name}@{origin}")
        if existing is not None:
            # Two requests pended side by side (an attach and a session join,
            # or two session joins) and the other grant built the mirror
            # first. Same mesh, same authority: this grant is merged into it.
            if primary not in existing.peers:
                raise MeshConflict(
                    f"mesh {mesh_name}@{origin} appeared here while the join "
                    "was pending — remove it and re-join"
                )
            return self._merge_grant(
                existing, primary, reply_token, grant, attach=attach
            )
        try:
            project = projects.require(project).name
        except projects.ProjectError:
            project = ""
        mesh = Mesh(mesh_name, me=self.machine, origin=origin, project=project)
        # The authority's rank list is authoritative; fall back to a plain
        # two-node order when talking to a daemon that predates phase 7.
        peers = [str(p) for p in (grant.get("peers") or []) if p]
        mesh.peers = peers or [primary]
        if primary not in mesh.peers:  # defensive: rank 0 must be the granter
            mesh.peers.insert(0, primary)
        if mesh.me and mesh.me not in mesh.peers:
            mesh.peers.append(mesh.me)  # never at rank 0 — we are the joiner
        try:
            mesh.authority_epoch = int(grant.get("epoch") or 0)
        except (TypeError, ValueError):
            mesh.authority_epoch = 0
        mesh.links[primary] = {
            "token_out": str(grant.get("token") or ""),
            "token_in": reply_token,
            "created_at": utcnow(),
            "enabled": True,
        }
        # Edges to the other peers, brokered by the authority: with them the
        # newcomer is part of the complete graph from its very first message.
        self._apply_link_grants(mesh, grant.get("links") or [], sender=primary)
        for entry in grant.get("members") or []:
            if isinstance(entry, dict) and entry.get("handle"):
                member = Member.from_dict(entry)
                mesh.members[member.handle] = member
        grant_edges = grant.get("member_edges")
        if isinstance(grant_edges, dict):
            mesh.member_edges = {str(k): bool(v) for k, v in grant_edges.items()}
        for m in grant.get("messages") or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            mesh.messages.append(m)
            mesh.seen_ids.add(str(m["id"]))
            self._append_log(mesh, m)
        mesh.policy = mesh_policy.load_policy(grant.get("policy"))
        # The vocabulary comes with the grant so a newcomer reads the roster's
        # role names correctly from its very first render, rather than after
        # whatever sync happens to carry the role set next.
        grant_roles = grant.get("roles")
        if isinstance(grant_roles, dict):
            mesh.set_roles_doc(
                mesh_roles.load_override(grant_roles.get("doc")),
                version=grant_roles.get("version"),
            )
        if attach:
            self._meshes[mesh.name] = mesh
            self._persist_def(mesh)
            self._persist_cursors(mesh)
            self._ensure_worker(mesh.name)
            self._drop_offer(mesh_name, primary)
            log.info("mesh %r: attached (mirror of %r)", mesh_name, primary)
            return None
        member_doc = grant.get("member") or {}
        member = mesh.members.get(str(member_doc.get("handle") or ""))
        if member is None:
            raise MeshError("grant is missing our member record")
        try:
            cursor = int(grant.get("cursor"))
        except (TypeError, ValueError):
            cursor = len(mesh.messages)
        mesh.cursors[member.handle] = cursor
        self._meshes[mesh.name] = mesh
        self._persist_def(mesh)
        self._persist_cursors(mesh)
        self._ensure_worker(mesh.name)
        self._brief_soon(mesh, member)
        log.info(
            "mesh %r: joined as %r (mirror of %r)",
            mesh_name, member.handle, primary,
        )
        return member

    def _merge_grant(
        self, mesh: Mesh, primary: str, reply_token: str, grant: dict,
        *, attach: bool,
    ) -> Optional[Member]:
        """A grant for a mirror that is already here (see _adopt_grant).

        The authority re-minted the link when it approved this request, so
        the grant's credentials replace the ones the mirror holds; keeping
        the old pair would leave both sides believing in a link neither can
        authenticate on. The roster, rank list and edges are the grant's,
        as a sync would carry them.
        """
        previous = mesh.links.get(primary) or {}
        mesh.links[primary] = {
            "token_out": str(grant.get("token") or ""),
            "token_in": reply_token,
            "created_at": utcnow(),
            "enabled": bool(previous.get("enabled", True)),
        }
        peers = [str(p) for p in (grant.get("peers") or []) if p]
        if peers:
            mesh.peers = peers
            if primary not in mesh.peers:
                mesh.peers.insert(0, primary)
            if mesh.me and mesh.me not in mesh.peers:
                mesh.peers.append(mesh.me)
        self._apply_link_grants(mesh, grant.get("links") or [], sender=primary)
        members = [
            Member.from_dict(e) for e in grant.get("members") or []
            if isinstance(e, dict) and e.get("handle")
        ]
        if members:
            mesh.members = {m.handle: m for m in members}
        grant_edges = grant.get("member_edges")
        if isinstance(grant_edges, dict):
            mesh.member_edges = {str(k): bool(v) for k, v in grant_edges.items()}
        for m in grant.get("messages") or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            if str(m["id"]) in mesh.seen_ids:
                continue
            mesh.messages.append(m)
            mesh.seen_ids.add(str(m["id"]))
            self._append_log(mesh, m)
        if attach:
            self._persist_def(mesh)
            self._persist_cursors(mesh)
            self._drop_offer(mesh.wire_name, primary)
            log.info("mesh %r: attach grant merged (already mirrored)", mesh.name)
            return None
        member_doc = grant.get("member") or {}
        member = mesh.members.get(str(member_doc.get("handle") or ""))
        if member is None:
            raise MeshError("grant is missing our member record")
        try:
            cursor = int(grant.get("cursor"))
        except (TypeError, ValueError):
            cursor = len(mesh.messages)
        mesh.cursors[member.handle] = cursor
        self._persist_def(mesh)
        self._persist_cursors(mesh)
        self._brief_soon(mesh, member)
        log.info(
            "mesh %r: joined as %r (grant merged into the mirror here)",
            mesh.name, member.handle,
        )
        return member

    # -- daemon attach: a daemon joins a mesh with no member of its own --- #
    async def attach(
        self, address: str, *, code: Optional[str] = None, project: str = "",
    ) -> dict:
        """Daemon-level join of ``mesh@machine`` (docs/mesh-design.md
        "Daemon attach").

        Builds the mirror and the link, and no member. Once attached, every
        session here joins by the bare mesh name without another approval —
        the link is what the owner approved. Pre-approval comes from ``code``
        (an invite ticket) or from an offer the owner pushed to this daemon;
        without either the request pends on the owner like a session join.
        Attaching a mesh that is already mirrored here is a no-op.

        ``project`` files the mirror here (blank = the default); an unknown
        project is refused, as :meth:`create` refuses one.
        """
        mesh_name, primary, invite_token = self._parse_addr(address, code)
        try:
            project = projects.require(project).name
        except projects.ProjectError as exc:
            raise MeshError(str(exc)) from None
        if not primary:
            raise MeshError(
                f"attach takes a remote address 'mesh@machine', not {address!r}"
            )
        if self._is_me(primary):
            raise MeshError(
                f"{mesh_name}@{primary} is this daemon's own — it owns its meshes"
            )
        # A local mesh of the same name is another mesh, not a conflict.
        existing = self._find(f"{mesh_name}@{primary}")
        if existing is not None:
            return self._attach_view(existing, already=True)
        machine = self._require_machine()
        if self.peer_transport is None:
            raise MeshError("relay uplink is not running — cannot reach peers")
        for rec in self._outgoing.values():
            if (rec.get("attach") and rec.get("mesh") == mesh_name
                    and rec.get("primary") == primary):
                return {
                    "pending": True, "request_id": rec["request_id"],
                    "mesh": mesh_name, "primary": primary,
                }
        reply_token = secrets.token_urlsafe(18)
        body = {
            "mesh": mesh_name,
            "machine": machine,
            "session": "",
            "handle": "",
            "role": "",
            "reply_token": reply_token,
        }
        if invite_token:
            body["code"] = invite_token
        offer = self._offers.get(f"{mesh_name}@{primary}")
        if offer:
            body["offer"] = str(offer.get("token") or "")
        resp = await self.peer_transport(primary, "/peer/mesh/join_request", body)
        if not isinstance(resp, dict):
            raise MeshError("unexpected response from the primary")
        if resp.get("granted"):
            self._adopt_grant(
                mesh_name, primary, reply_token, resp.get("grant") or {},
                attach=True, project=project,
            )
            return self._attach_view(self.get(f"{mesh_name}@{primary}"))
        if resp.get("pending"):
            rid = str(resp.get("id") or "")
            self._outgoing[rid] = {
                "request_id": rid,
                "mesh": mesh_name,
                "primary": primary,
                "reply_token": reply_token,
                "attach": True,
                "session": "",
                "handle": "",
                "role": "",
                "project": project,
                "requested_at": utcnow(),
            }
            self._persist_outgoing()
            return {
                "pending": True, "request_id": rid,
                "mesh": mesh_name, "primary": primary,
            }
        raise MeshError("unexpected response from the primary")

    @staticmethod
    def _attach_view(mesh: Mesh, *, already: bool = False) -> dict:
        return {
            "attached": True,
            "already": already,
            "mesh": mesh.name,
            "name": mesh.wire_name,
            "origin": mesh.origin,
            "primary": mesh.primary,
            "project": mesh.project or projects.DEFAULT,
            "members": len(mesh.members),
        }

    async def detach(self, name: str, *, force: bool = False) -> dict:
        """Undo :meth:`attach`: tell the owner, then drop the mirror.

        The owner removes this daemon's link and every member it hosts, so
        the local sessions leave with it. ``force`` drops the mirror even
        when the owner cannot be told (it keeps a stale guest entry that its
        operator can revoke).
        """
        mesh = self.get(name)
        if not mesh.primary:
            raise MeshError(
                f"mesh {name!r} is owned by this daemon — delete it instead"
            )
        told = True
        try:
            await self._peer_call_primary(mesh, "/peer/mesh/detach", {})
        except PeerUnreachable as exc:
            if not force:
                raise MeshError(
                    f"cannot reach {mesh.primary!r} to detach ({exc}) — "
                    "retry, or force it to drop the mirror here only"
                ) from None
            told = False
        except MeshError as exc:
            # The owner answered and refused — typically it no longer knows
            # this daemon's link, so there is nothing left there to undo.
            if not force:
                raise MeshError(
                    f"{mesh.primary!r} refused the detach ({exc}) — force "
                    "it to drop the mirror here only"
                ) from None
            told = False
        primary = mesh.primary
        self._drop_mesh(name)
        log.info("mesh %r: detached from %r", name, primary)
        return {"mesh": name, "primary": primary, "notified": told}

    def peer_offer_accept(
        self, name: str, machine: str, token: str, *,
        cancel: bool = False, project: str = "", members: int = 0,
    ) -> dict:
        """An owner offers (or withdraws) one of its meshes to this daemon.

        The offer arrives over the relay addressed to *our* registered name,
        so only this daemon can hold the token; the claimed sender is not
        verified, but a forged offer carries a token its named owner will
        refuse, so it costs one refused attach and nothing more.
        """
        if not _NAME_RE.match(name or "") or not _NAME_RE.match(machine or ""):
            raise MeshError("invalid mesh offer")
        key = f"{name}@{machine}"
        if cancel:
            self._offers.pop(key, None)
            self._persist_offers()
            return {"ok": True, "cancelled": True}
        if not token:
            raise MeshError("mesh offer carries no token")
        if self._find(key) is not None:
            return {"ok": True, "attached": True}
        try:
            count = int(members)
        except (TypeError, ValueError):
            count = 0
        self._offers[key] = {
            "mesh": name,
            "machine": machine,
            "token": str(token),
            "project": str(project or ""),
            "members": count,
            "offered_at": utcnow(),
        }
        self._persist_offers()
        log.info("mesh %r: offered to this daemon by %r", name, machine)
        return {"ok": True}

    def offers_received(self) -> List[dict]:
        """Offers pushed to this daemon, without their tokens."""
        return [
            {k: v for k, v in rec.items() if k != "token"}
            for rec in sorted(
                self._offers.values(),
                key=lambda r: (r.get("machine", ""), r.get("mesh", "")),
            )
        ]

    def _drop_offer(self, name: str, machine: str) -> None:
        if self._offers.pop(f"{name}@{machine}", None) is not None:
            self._persist_offers()

    def _persist_offers(self) -> None:
        root = self._mesh_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
            path = root / "mesh_offers.json"
            with atomic.scratch(path) as tmp:
                tmp.write_text(
                    json.dumps(list(self._offers.values()), indent=2),
                    encoding="utf-8",
                )
                atomic.replace(tmp, path)
        except OSError as exc:
            log.warning("cannot persist mesh offers: %s", exc)

    def peer_meshes_list(self) -> List[dict]:
        """``/peer/meshes``: the public meshes this daemon owns.

        Only meshes this daemon is the authority of — never a mirror, never
        an offer received from elsewhere — so discovery reaches exactly one
        relay hop and a mesh is listed only by the daemon that owns it.
        """
        return [
            {
                "mesh": mesh.wire_name,
                "project": mesh.project,
                "members": len(mesh.members),
                "peers": max(len(mesh.peers), 1),
                "created_at": mesh.created_at,
            }
            for mesh in sorted(self._meshes.values(), key=lambda m: m.name)
            if not mesh.origin and not mesh.primary and mesh.visibility == "public"
        ]

    async def discover(self, *, timeout: float = 8.0) -> dict:
        """Meshes this daemon could attach: the union, over every daemon on
        every connected relay, of the public meshes it owns, plus the offers
        pushed here.

        One hop only: each row is keyed by the daemon that answered, which
        lists only what it owns, so nothing is relayed onward.
        """
        rows: Dict[str, dict] = {}
        errors: Dict[str, str] = {}
        names: List[str] = []
        if self.peer_lister is None:
            errors["relay"] = "relay uplink is not running"
        else:
            try:
                names = list(await self.peer_lister())
            except Exception as exc:  # noqa: BLE001 — surface as a row error
                errors["relay"] = str(exc) or type(exc).__name__

        async def ask(peer: str):
            try:
                return peer, await asyncio.wait_for(
                    self.peer_transport(peer, "/peer/meshes", {}), timeout
                )
            except Exception as exc:  # noqa: BLE001 — one peer, one error
                return peer, exc

        if names and self.peer_transport is not None:
            for peer, res in await asyncio.gather(*(ask(p) for p in names)):
                if isinstance(res, BaseException):
                    errors[peer] = str(res) or type(res).__name__
                    continue
                for entry in (res or {}).get("meshes") or []:
                    if not isinstance(entry, dict):
                        continue
                    name = str(entry.get("mesh") or "")
                    if not _NAME_RE.match(name):
                        continue
                    rows[f"{name}@{peer}"] = {
                        "mesh": name,
                        "machine": peer,
                        "project": str(entry.get("project") or ""),
                        "members": entry.get("members"),
                        "peers": entry.get("peers"),
                        "sources": ["public"],
                    }
        for key, offer in self._offers.items():
            row = rows.setdefault(key, {
                "mesh": offer["mesh"],
                "machine": offer["machine"],
                "project": str(offer.get("project") or ""),
                "members": offer.get("members"),
                "peers": None,
                "sources": [],
            })
            row["sources"].append("offer")
        pending = {
            (r.get("mesh"), r.get("primary")): r.get("request_id")
            for r in self._outgoing.values() if r.get("attach")
        }
        for row in rows.values():
            row["address"] = f"{row['mesh']}@{row['machine']}"
            # Keyed by address, a local mesh of the same name is a different
            # mesh: nothing here can be "taken".
            local = self._find(row["address"])
            if local is not None:
                row["state"] = "attached"
                row["key"] = local.name  # what this daemon calls it
            elif (row["mesh"], row["machine"]) in pending:
                row["state"] = "pending"
                row["request_id"] = pending[(row["mesh"], row["machine"])]
            else:
                row["state"] = "available"
            # An offer carries its own pre-approval; a public listing does
            # not, so attaching from it waits for the owner's approval.
            row["access"] = "offer" if "offer" in row["sources"] else "approval"
        return {
            "meshes": sorted(
                rows.values(), key=lambda r: (r["machine"], r["mesh"])
            ),
            "peers": sorted(names),
            "errors": errors,
        }

    async def _join_local(
        self,
        name: str,
        session: str,
        *,
        handle: str = "",
        role: str = "",
        subroles: Sequence[str] = (),
    ) -> Member:
        """Member join into a mesh already present here (owned or mirror).

        On a mirror this is a *request*: the primary is the sole authority on
        handle uniqueness, so the join is forwarded and fails fast when the
        primary is unreachable (membership never queues).
        """
        mesh = self.get(name)
        try:
            live = self.manager.get(session)
        except ManagerError:
            raise MeshError(f"no session named {session!r} on this daemon") from None
        if live.exited:
            raise MeshError(f"session {session!r} has exited — respawn it first")
        handle = (handle or session).strip()
        if not _NAME_RE.match(handle):
            raise MeshError(
                f"invalid handle {handle!r}: use letters, digits, '.', '_' or '-'"
            )
        if handle in mesh.members:
            raise MeshConflict(f"handle {handle!r} is already taken in mesh {name!r}")
        for m in mesh.members.values():
            if self._is_local(mesh, m) and m.session == session:
                raise MeshConflict(
                    f"session {session!r} is already in mesh {name!r} as {m.handle!r}"
                )
        if mesh.primary:
            payload = await self._peer_call_primary(
                mesh,
                "/peer/mesh/join",
                {
                    "session": session,
                    "handle": handle,
                    "role": role,
                    "subroles": list(subroles),
                    # The authority wires the member, but only this daemon can
                    # see the session tree behind it — and the lineage it will
                    # eventually learn from our sync acks has not been sent
                    # yet. Carried on the join so a child spawned across a
                    # machine boundary is wired to its parent and not, for one
                    # sync interval, mistaken for a root.
                    "parent": self.parent_handle_for(mesh, session),
                },
            )
            member = Member.from_dict(payload)
            mesh.members[member.handle] = member
            # ...and the edges its join just decided, for the same reason the
            # grant carries them: we resolve this member's recipients here,
            # before forwarding, and would refuse its first send otherwise.
            edges = payload.get("member_edges")
            if isinstance(edges, dict):
                mesh.member_edges = {str(k): bool(v) for k, v in edges.items()}
            # The primary tells us its log length at join time: messages
            # sequenced before the join never deliver to this member, even
            # ones still in flight to this mirror.
            mesh.cursors[member.handle] = int(
                payload.get("cursor") or len(mesh.messages)
            )
            self._persist_def(mesh)
            self._persist_cursors(mesh)
            self._brief_soon(mesh, member)
            return member
        # The parent is read BEFORE the member exists — `parent_handle_for`
        # walks the session tree, and the joining session is not yet in the
        # roster to be found by it.
        parent = self.parent_handle_for(mesh, session)
        member = self._new_member(mesh, handle, session, role, subroles)
        # Absolute from birth on a federated mesh, for the reason in
        # _absolutize_roster: that runs when a mesh federates, at a handover
        # and on migration — none of which is a join, so a member enrolled
        # AFTER federation stayed blank and every mirror read it as its own.
        # `me`, not `authority`: this path only ever enrols OUR sessions. A
        # guest's join is the authority's `peer_join_accept`, which is handed
        # the guest's machine and names it outright.
        if mesh.peers and mesh.me:
            member.machine = mesh.me
        mesh.members[handle] = member
        self._wire_member(mesh, member, parent)
        # New members start caught up: joining must not replay the backlog.
        mesh.cursors[handle] = len(mesh.messages)
        self._persist_cursors(mesh)
        self._roster_changed(mesh)
        self._brief_soon(mesh, member)
        return member

    @contextlib.contextmanager
    def defer_briefing(self, session: str):
        """Hold back this session's automatic join briefing for one join.

        Onboarding composes the briefing into one opening block together with
        the workflow assignment and the opening task, so the paste this would
        schedule would be a second one racing it. Scoped to a context manager
        rather than a flag on ``join`` because three different join paths
        (local, mirror, remote grant) all end in a briefing, and every one of
        them should be held back by the same statement.
        """
        self._brief_deferred.add(session)
        try:
            yield
        finally:
            self._brief_deferred.discard(session)

    def _brief_soon(self, mesh: Mesh, member: Member) -> None:
        """Schedule a join briefing injection into the new member's terminal."""
        if member.session in self._brief_deferred:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.ensure_future(self._brief(mesh, member))

    def _stance_lines(
        self, mesh: Mesh, member: Member, *, inline: bool = True
    ) -> str:
        """The role stance supplied by every local member's briefing.

        Role is a mesh membership property and every harness receives the
        same opening block.  The full stance therefore arrives here, named by
        the content id that later session reminders can check and
        :func:`rebrief.recall` can serve.  ``inline=False`` remains the
        rebrief budget fallback, and a remote member is left to the daemon
        hosting its terminal.
        """
        role = mesh.roleset.get(member.role)
        if not (role and role.stance.strip()):
            return ""
        pointer = (
            f"stance: run 'claunch mesh stance {mesh.name}' now — it prints "
            f"what a {member.role} is on this mesh, and it is binding\n"
        )
        if not inline or not self._is_local(mesh, member):
            # The caller has a harder budget than the join does and would
            # rather cut this than anything else it carries. Right ordering:
            # the stance is the one section with a guaranteed alternative
            # one command away — the owed ledger and the opening task have
            # none. See :func:`rebrief.compose`.
            return pointer
        whole = role.stance.strip()
        body = whole
        if len(body) > _INLINE_STANCE:
            body = body[:_INLINE_STANCE].rstrip() + " [...]"
        # The id names the WHOLE stance, not the copy below it, which may
        # have been cut at :data:`_INLINE_STANCE`. That is the point rather
        # than a discrepancy: an agent that finds the id here knows it still
        # has a starting position, and one that wants the part the cap took
        # pulls it by the same id (:func:`rebrief.recall`). It rides here and
        # only here, next to the prose, because an id delivered on its own
        # would let "is this id in my context?" answer yes for text that
        # never arrived.
        ident = digests.text_digest(whole)
        marker = f" [text id: {ident}]" if ident else ""
        return (
            f"{pointer}stance ({member.role}), binding{marker}:\n"
            f"{body}\n"
        )

    def stance_given(self, mesh: Mesh, member: Member) -> bool:
        """Whether this daemon supplied the stance and its id to the member."""
        return self._is_local(mesh, member)

    def briefing_block(
        self, mesh: Mesh, member: Member, *, inline_stance: bool = True
    ) -> str:
        """The briefing's *text*: what this member is here, and how to speak.

        Split from :meth:`_brief` — which waits for idle and pastes it — so a
        session being onboarded can fold it into one opening block instead of
        having it arrive as a second paste racing the first.

        Built at call time and never stored, because it names only the peers
        this member can reach *now*. A briefing that listed the whole roster
        would have a spawned child addressing peers it is not connected to,
        and reading the refusal as a bug rather than as the arrangement it was
        started under. (This is also why it is the briefing that carries the
        roster and never the system prompt: the graph is rewired mid-session,
        an appended prompt is not.)
        """
        # Membership survives session exit so history and queued mail remain
        # available after respawn. The briefing should offer current peers,
        # excluding local sessions known to have exited or been removed.
        # Remote session liveness is unknown here; keep those members.
        #
        # "Known to have exited or been removed" is `_member_category`'s
        # question, so it is asked in the one place that answers it rather
        # than re-derived from `session.exited` here: that category is
        # ``running``, ``killed``, ``paused``, ``archived`` or ``missing``,
        # and the two a briefing keeps are the running ones and ``remote``
        # (unknowable, so not called dead). Deriving it twice is how the
        # roster, the filter above it and this briefing would come to
        # disagree about which peers are still around.
        available = {
            handle
            for handle, peer in mesh.members.items()
            if handle != member.handle
            and self._member_category(mesh, peer) in ("running", "remote")
        }
        reachable = [h for h in mesh.neighbours(member.handle) if h in available]
        others = ", ".join(
            f"{h} ({mesh.members[h].role_label()})" for h in reachable
        ) or "(nobody else yet)"
        hidden = len(available) - len(reachable)
        return (
            "---\n"
            "# claunch mesh: join briefing -- machine-generated, not typed by the user\n"
            f"mesh: {mesh.name}\n"
            f"you: {member.handle} (role: {member.role})\n"
            + self._project_line(mesh, member)
            + (
                f"subroles: {', '.join(member.subroles)} — you also answer "
                "for these roles when a workflow or a peer looks one up\n"
                if member.subroles else ""
            ) +
            f"members: {others}\n"
            + (
                f"note: {hidden} other member(s) exist that you are not "
                "connected to and cannot message\n" if hidden > 0 else ""
            ) +
            f"send: claunch mesh send {mesh.name} <to|*> \"...\"\n"
            + self._stance_lines(mesh, member, inline=inline_stance) +
            f"protocol: activate your 'mesh' skill NOW (/mesh {mesh.name}) to "
            "load the member protocol; if you have no such skill, run "
            "'claunch install' first and retry\n"
            f"note: incoming mesh messages will be typed into this terminal\n"
            "---"
        )

    def _project_line(self, mesh: Mesh, member: Member) -> str:
        """The ``project:`` line of the briefing, or ``""`` for a member whose
        record this daemon does not hold (remote, or gone).

        The session's own project, not the mesh's: they usually agree, but
        the line answers "which project am I filed under" — the scope
        ``claunch sessions`` and ``claunch mesh ls`` narrow to inside the
        session — and that is a fact about the session. The hint about
        ``--project all`` rides here because the briefing is the one block a
        member is guaranteed to read; s769 (2026-09-24) listed every
        default-project session because nothing had ever told it there was
        a project to be in.
        """
        if not self._is_local(mesh, member):
            return ""
        try:
            sdef = self.manager.get(member.session).sdef
        except ManagerError:
            return ""
        project = projects.normalize(getattr(sdef, "project", None))
        return (
            f"project: {project} -- 'claunch sessions' and 'claunch mesh ls' "
            f"show this project only; add '--project {projects.ALL}' for every "
            "project\n"
        )

    async def _brief(self, mesh: Mesh, member: Member, *, hold: float = 30.0) -> None:
        """Idle-gated briefing paste: who you are here and how to speak.

        A self-join (agent ran ``claunch mesh join`` in its own terminal) sees
        the CLI output too; the briefing matters for members enrolled from the
        web, whose agent would otherwise never learn it joined anything.
        """
        deadline = time.monotonic() + hold
        while time.monotonic() < deadline:
            try:
                session = self.manager.get(member.session)
            except ManagerError:
                return
            if member.handle not in mesh.members:
                return  # left before the briefing landed
            if session.exited:
                return
            if session.status() == STATUS_IDLE and not session.keyboard_busy():
                break
            await asyncio.sleep(0.5)
        else:
            return  # never went idle; skip rather than interleave
        # Retried inside the same window rather than fired once: deliver()
        # returns False when a human is composing at that terminal (it does
        # not type over them), and a briefing spent on that moment is a
        # member who never learns it joined anything.
        block = self.briefing_block(mesh, member)
        while time.monotonic() < deadline:
            if await session.deliver(block):
                return
            if session.exited or member.handle not in mesh.members:
                return
            await asyncio.sleep(0.5)

    async def leave(self, name: str, handle: str) -> Member:
        """Remove a member. Guests may only remove their OWN members (the
        request is forwarded); the primary may remove anyone (kick)."""
        mesh = self.get(name)
        member = mesh.members.get(handle)
        if member is None:
            raise MeshError(f"no member {handle!r} in mesh {name!r}")
        if mesh.primary:
            # Same shape as `_is_local`'s mirror arm, blank relay name and
            # all: without the first clause an unnamed daemon compares "" to
            # "" and waves a blank row through. `_peer_call` refuses that
            # case a line later, so this is agreement rather than a fix — but
            # a guard that reads differently from the rule it enforces is how
            # the two got out of step to begin with.
            if not (bool(self.machine) and member.machine == self.machine):
                raise MeshError(
                    f"{handle!r} is not a member from this daemon — the "
                    f"primary ({mesh.primary}) owns the roster"
                )
            await self._peer_call_primary(
                mesh, "/peer/mesh/leave", {"handle": handle}
            )
        mesh.members.pop(handle, None)
        mesh.cursors.pop(handle, None)
        mesh._first_pending.pop(handle, None)
        # Handles are reusable, so a rejoining name must not inherit the
        # write-offs made against whoever wore it last — the same reason the
        # member edges below are pruned.
        mesh.dismissed.pop(handle, None)
        mesh.stranded_told.pop(handle, None)
        for told in mesh.stranded_told.values():
            if handle in told:
                told.remove(handle)
        mesh._stranded_scan.clear()
        # A reply receipt belongs to this member instance as well.  A reused
        # handle must not inherit a request or sender notification from the
        # session that previously held it.
        mesh.response_watches = {
            key: watch for key, watch in mesh.response_watches.items()
            if watch.get("from") != handle and watch.get("to") != handle
        }
        # Edges naming a departed member go with it: handles are reusable, so
        # a rejoining name would otherwise inherit the isolation imposed on
        # whoever wore it last — a member that mysteriously cannot reach
        # anyone, with nothing in the roster to explain it.
        mesh.prune_member_edges()
        if mesh.primary:
            self._persist_def(mesh)
            self._persist_cursors(mesh)
        else:
            mesh.remote_activity.pop(handle, None)
            mesh.remote_lineage.pop(handle, None)
            self._drop_leases(mesh, handle)
            self._persist_cursors(mesh)
            self._roster_changed(mesh)
        return member

    def _drop_leases(self, mesh: Mesh, handle: str) -> None:
        """Authority side: a departed member's leases go with it.

        A key held by a handle nobody wears any more would sit until its
        TTL ran out, and a rejoining session wearing the same handle would
        inherit a lock it never took.
        """
        if mesh.leases.release_all(handle):
            self._persist_leases(mesh)

    def _roster_changed(self, mesh: Mesh) -> None:
        """Authority-side roster bump: persist and fan out to peers soon."""
        mesh.roster_version += 1
        self._ensure_pair_links(mesh)
        self._persist_def(mesh)
        self._flush_guests_soon(mesh)

    def resolve_sender(self, name: str, sender: str) -> Optional[Member]:
        """Resolve a handle *or* a local session name to a member."""
        mesh = self.get(name)
        if sender in mesh.members:
            return mesh.members[sender]
        for m in mesh.members.values():
            if self._is_local(mesh, m) and m.session == sender:
                return m
        return None

    def member_for_session(self, mesh: Mesh, session: str) -> Optional[Member]:
        """The member THIS daemon's ``session`` wears in ``mesh``.

        "Which member am I?" is the daemon's question to answer, because the
        rule is :meth:`_is_local` and a blank ``machine`` means opposite
        things either side of ``primary``. A caller matching on blankness
        finds nobody on a mirror — our own members are always stamped there —
        and on the authority finds only the rows no stamping pass has reached
        yet, so the same command works or fails by who joined when.

        Deliberately not :meth:`resolve_sender`, which tries ``sender`` as a
        handle first: that is right for a peer the caller named and wrong
        here, where a session whose name matches somebody else's handle would
        be told it is that member.
        """
        if not session:
            return None
        for m in mesh.members.values():
            if self._is_local(mesh, m) and m.session == session:
                return m
        return None

    # ------------------------------------------------------------------ #
    # messaging
    # ------------------------------------------------------------------ #
    async def send(
        self,
        name: str,
        sender: str,
        to: Union[str, List[str]],
        body: str,
        *,
        external: bool = False,
        type: str = "say",
        reply_to: Optional[str] = None,
        sections: Optional[dict] = None,
        ref: Optional[dict] = None,
    ) -> dict:
        """Send a message into the mesh.

        ``sender`` is a member handle or a local session name; ``external``
        admits a non-member sender (the human on the dashboard). ``to`` is
        ``"*"``, a handle, or a list of handles. ``type`` is the message
        *intent* (``say``/``ask`` invite a reply; ``fyi``/``ack``/``decide`` do
        not). ``reply_to`` threads this message to an earlier message id, and
        ``ref`` points at whatever the message is *about* — carried verbatim,
        never interpreted (see :data:`REF_KEY`).

        ``sections`` turns this into a BATCH send: ``{handle: text}`` (or
        ``{handle: {text, type}}``) of per-recipient addenda. ``body`` becomes
        the shared preamble; each recipient is *delivered* only ``body`` plus
        its own section, while the log keeps the full composite as ONE
        message (one id, one entry). A section may carry its own intent.

        On the primary the message is sequenced into THE log immediately. On
        a mirror it is forwarded to the primary — every message is, even one
        between two members of this same daemon, so all histories stay
        identical — and queued durably in the outbox when the primary is
        unreachable (result carries ``queued: True``).
        """
        mesh = self.get(name)
        to, unreached = await self._resolve_audience(mesh, sender, to)
        if mesh.primary:
            result = await self._send_from_mirror(
                mesh, sender, to, body, external=external, type=type,
                reply_to=reply_to, sections=sections, ref=ref,
            )
        else:
            result = self._send_core(
                mesh, sender, to, body, external=external, type=type,
                reply_to=reply_to, sections=sections, ref=ref,
            )
            self._flush_guests_soon(mesh)
        if unreached:
            note = (
                f"not reached by the selector: {', '.join(unreached)} "
                "(assignee of an issue in that state, but no connected member "
                "of this mesh runs that session)"
            )
            prior = result.get("notice")
            result = {**result, "notice": f"{prior} {note}" if prior else note}
        return result

    # ------------------------------------------------------------------ #
    # urgent one-shot send
    # ------------------------------------------------------------------ #
    def _urgent_target(
        self, home: Mesh, to: str, target_mesh: str
    ) -> Tuple[Mesh, Member]:
        """Which member of which mesh ``to`` names, on THIS daemon.

        ``to`` is a handle in the sender's own mesh, or a session name in any
        mesh here. A member whose session lives on another machine, or whose
        mesh this daemon only mirrors, is refused by name: the urgent path
        types into a terminal this daemon owns and appends to a log this
        daemon is the authority for, and it has neither for those.
        """
        meshes = [self.get(target_mesh)] if target_mesh else list(self._meshes.values())
        hits: List[Tuple[Mesh, Member]] = []
        elsewhere: List[str] = []
        for mesh in meshes:
            for member in mesh.members.values():
                if not (member.session == to or (mesh is home and member.handle == to)):
                    continue
                if mesh.primary or not self._is_local(mesh, member):
                    elsewhere.append(f"{member.handle} in {mesh.name}")
                else:
                    hits.append((mesh, member))
        if not hits:
            if elsewhere:
                raise MeshError(
                    f"{to!r} lives on another machine ({', '.join(elsewhere)}): "
                    "an urgent send is delivered on this daemon only, with no "
                    "relay hop — ask the operator to reach it"
                )
            raise MeshError(
                f"no member named {to!r} in any mesh on this daemon (a handle "
                f"in {home.name!r}, or a session name)"
            )
        own = [h for h in hits if h[0] is home]
        if len(own) == 1:
            return own[0]
        if len(hits) > 1:
            raise MeshError(
                f"{to!r} is a member of {len(hits)} meshes here "
                f"({', '.join(sorted(m.name for m, _ in hits))}) — name one "
                "with target_mesh"
            )
        return hits[0]

    def _urgent_rate_check(self, sender: str, to_key: str) -> None:
        """3 sends per sender per hour, 1 per sender/target pair per 10 min."""
        now = time.monotonic()
        recent = [t for t in self._urgent_sent.get(sender, []) if now - t < URGENT_SENDER_WINDOW]
        self._urgent_sent[sender] = recent
        if len(recent) >= URGENT_PER_SENDER:
            wait = int(URGENT_SENDER_WINDOW - (now - recent[0]))
            raise MeshError(
                f"urgent send limit: {URGENT_PER_SENDER} per hour per sender "
                f"already used — next one allowed in {wait}s"
            )
        last = self._urgent_pair.get((sender, to_key))
        if last is not None and now - last < URGENT_PAIR_GAP:
            raise MeshError(
                f"urgent send limit: {sender!r} already sent {to_key!r} one "
                f"{int(now - last)}s ago — one per pair per "
                f"{int(URGENT_PAIR_GAP // 60)} minutes"
            )

    def urgent_send(
        self,
        name: str,
        sender: str,
        to: str,
        body: str,
        *,
        reason: str,
        target_mesh: str = "",
    ) -> dict:
        """One message to one member the sender is not connected to.

        The exception path for a refusal the member graph would otherwise
        stand on (``_resolve_recipients``): the sender says why, once, and
        the message is delivered and recorded. What it deliberately does not
        do: change ``member_edges``, file or grant a wire request, take a
        list or ``*`` or a selector, or read any caller-supplied
        ``external`` claim — authority comes from ``sender``, the caller's
        own session, and that member's role in mesh ``name``.

        ``sender`` empty means the human operator (the CLI sends no session
        outside a claunch session); an agent sender must hold ``leader`` in
        ``name``. Operators are not rate limited.

        KNOWN LIMIT, not enforced: the daemon cannot tell an agent's HTTP call
        from the operator's, so an agent that omits ``sender`` is treated as
        the unlimited operator. Agents are forbidden from doing that (the CLI
        and the MCP tool always send ``$CLAUNCH_SESSION``); the only
        safeguard is that ``authority: operator`` is written into both audit
        records, so misuse is visible. Same class as the restart gate (see
        CLAUDE.md).

        Delivery is an append to the TARGET mesh's log from a non-member
        label ``urgent:<sender>``, which ``Mesh.connected`` waves through the
        way it does any external sender. The sender's mesh gets an audit
        entry addressed to nobody when the target is in another mesh; the
        target mesh's copy is the message itself, and both carry
        ``ref.urgent``.
        """
        home = self.get(name)
        if not isinstance(to, str) or not to.strip() or to.strip() == "*" or to.startswith("@"):
            raise MeshError(
                "an urgent send names exactly one member (a handle or a "
                "session) — no '*', selector or list"
            )
        to = to.strip()
        reason = str(reason or "").strip()
        if len(reason) < URGENT_MIN_REASON:
            raise MeshError(
                f"an urgent send needs a reason of at least {URGENT_MIN_REASON} "
                "characters — it is written into both meshes' logs"
            )
        text = _CTRL_RE.sub("", str(body or "")).strip()
        if not text:
            raise MeshError("empty message body")
        if sender:
            member = self.resolve_sender(name, sender)
            if member is None or not self._is_local(home, member):
                raise MeshError(
                    f"{sender!r} is not a local member of mesh {name!r}: an "
                    "urgent send is an operator or leader act"
                )
            if not member.holds("leader"):
                raise MeshError(
                    f"{member.handle!r} is a {member.role_label()} in mesh "
                    f"{name!r}: only a leader or the operator may send an "
                    "urgent message — ask your leader to send it"
                )
            who, authority, own_session = member.handle, "leader", member.session
        else:
            who, authority, own_session = "operator", "operator", ""
        tmesh, target = self._urgent_target(home, to, target_mesh)
        if target.session == own_session and own_session:
            raise MeshError("an urgent send cannot address yourself")
        to_key = f"{tmesh.name}/{target.handle}"
        if authority != "operator":
            self._urgent_rate_check(who, to_key)
        record = {
            "from": who,
            "from_mesh": home.name,
            "to": target.handle,
            "to_session": target.session,
            "to_mesh": tmesh.name,
            "authority": authority,
            "reason": reason,
            "connected": bool(tmesh is home and home.connected(who, target.handle)),
        }
        head = (
            f"[URGENT one-shot from {who} ({authority}, mesh {home.name}) — "
            f"reason: {reason}]"
        )
        foot = (
            "(No channel was opened. This sender is not connected to you, so "
            "a reply to it is refused: answer through your own leader.)"
        )
        result = self._send_core(
            tmesh, f"urgent:{who}", target.handle, f"{head}\n{text}\n{foot}",
            external=True, type="fyi", ref={URGENT_REF_KEY: record},
        )
        self._flush_guests_soon(tmesh)
        now = time.monotonic()
        if authority != "operator":
            self._urgent_sent.setdefault(who, []).append(now)
            self._urgent_pair[(who, to_key)] = now
        audited = [tmesh.name]
        if tmesh is not home:
            audit = {
                "id": "msg-" + uuid.uuid4().hex[:12],
                "ts": utcnow(),
                "from": f"urgent:{who}",
                "to": [],
                "type": "fyi",
                "epoch": home.authority_epoch,
                "seq": home.next_seq,
                "body": (
                    f"URGENT SEND audit: {who} ({authority}) -> {target.handle} "
                    f"in mesh {tmesh.name} ({target.session}), message "
                    f"{result['id']}. Reason: {reason}"
                ),
                REF_KEY: {URGENT_REF_KEY: {**record, "delivered_id": result["id"]}},
            }
            home.next_seq += 1
            home.messages.append(audit)
            home.seen_ids.add(audit["id"])
            home.last_append = time.monotonic()
            self._append_log(home, audit)
            self._flush_guests_soon(home)
            audited.append(home.name)
        return {**result, "urgent": record, "audited_in": audited}

    async def _resolve_audience(
        self, mesh: Mesh, sender: str, to: Union[str, List[str]]
    ) -> Tuple[Union[str, List[str]], List[str]]:
        """Expand an audience selector in ``to`` into the handles it names.

        Anything that is not a selector passes through unchanged. A selector
        becomes the connected LOCAL members whose session is an assignee of
        an issue in the selector's board state — the board is this daemon's,
        so a guest member's session name says nothing about it. Returns the
        address and the assignee sessions left out of it (not a member here,
        or not connected to the sender), which the send reports rather than
        dropping silently.
        """
        if not (isinstance(to, str) and to.startswith("@")):
            return to, []
        status = AUDIENCE_SELECTORS.get(to)
        if status is None:
            raise MeshError(
                f"unknown audience selector {to!r} — known: "
                + ", ".join(sorted(AUDIENCE_SELECTORS))
            )
        if self.audience_resolver is None:
            raise MeshError(
                f"audience selector {to!r} needs the board, and this daemon "
                "has none wired — name the handles instead"
            )
        member = self.resolve_sender(mesh.name, sender)
        from_handle = member.handle if member is not None else sender
        session = member.session if member is not None else sender
        sessions = set(await self.audience_resolver(session, status))
        handles = sorted(
            m.handle for m in mesh.members.values()
            if m.session in sessions
            and self._is_local(mesh, m)
            and m.handle != from_handle
            and mesh.connected(from_handle, m.handle)
        )
        reached = {mesh.members[h].session for h in handles}
        unreached = sorted(s for s in sessions if s not in reached and s != session)
        if not handles:
            raise MeshError(
                f"{to} resolves to nobody you can reach in mesh {mesh.name!r}: "
                f"no connected member's session holds an issue in {status!r}"
                + (f" (assignees not reached: {', '.join(unreached)})" if unreached else "")
                + " — there is no one this message is for, so nothing was sent"
            )
        return handles, unreached

    def _send_core(
        self,
        mesh: Mesh,
        sender: str,
        to: Union[str, List[str]],
        body: str,
        *,
        external: bool = False,
        type: str = "say",
        reply_to: Optional[str] = None,
        sections: Optional[dict] = None,
        ref: Optional[dict] = None,
        msg_id: Optional[str] = None,
        sender_machine: Optional[str] = None,
        ts: Optional[str] = None,
    ) -> dict:
        """The authoritative append path — primary/owned meshes only.

        ``msg_id`` preserves a guest-generated id (dedupe makes upstream
        retries idempotent); ``sender_machine`` pins which guest the sender
        must belong to when the send arrived over the wire.
        """
        member = self.resolve_sender(mesh.name, sender)
        if member is None and not external:
            raise MeshError(
                f"{sender!r} is neither a handle nor a member session in mesh "
                f"{mesh.name!r} (join first: claunch mesh join {mesh.name})"
            )
        if member is not None and sender_machine is not None:
            if member.machine != sender_machine:
                raise MeshError(
                    f"sender {sender!r} is not a member of daemon {sender_machine!r}"
                )
        if (
            member is not None
            and not external
            and sender_machine is None
            and not self._is_local(mesh, member)
        ):
            # No impersonation: a locally-issued send may only speak as a
            # member whose session lives here. The operator speaks as
            # themselves via external=True.
            raise MeshError(
                f"{member.handle!r} lives on {member.machine!r} — send from "
                "that daemon, or speak as yourself with an external send"
            )
        from_handle = member.handle if member is not None else sender
        body = _CTRL_RE.sub("", str(body)).strip()
        # Read before backpressure narrows ``to`` to a list. The human at the
        # dashboard is not warned: the advisory is about agents spending
        # other agents' turns.
        broadcast = to == "*" and not external
        recipients = self._resolve_recipients(mesh, from_handle, to)
        if not recipients:
            raise MeshError(self._nobody_to_deliver_to(mesh, from_handle))
        norm_sections = _normalize_sections(sections, recipients, from_handle)
        if norm_sections is None and not body:
            raise MeshError("empty message body")
        if norm_sections is not None:
            for rcpt in recipients:
                sec = norm_sections.get(rcpt)
                if not _slice_body(body, sec["text"] if sec else None):
                    raise MeshError(
                        f"recipient {rcpt!r} would receive an empty message — "
                        "give a shared body, a section for them, or drop them "
                        "from 'to'"
                    )
        intent = str(type or "say").strip().lower() or "say"
        # ---- backpressure: the door ---------------------------------- #
        # Everything above decided whether the message is well-formed and
        # who it is FOR; this decides whether they can take it. A recipient
        # whose backlog has already reached ``inbox_max`` is not accepting,
        # and the honest answer to its sender is "not delivered, try again"
        # — said here, synchronously, while the sender can still do
        # something else with the turn it was about to spend.
        #
        # Two carve-outs, both deliberate:
        #  * an EXTERNAL sender is the human at the dashboard. They are not
        #    the fan-in this gate exists to bound, they send one message and
        #    read the answer, and they already have "deliver now" for the
        #    backlog. Turning a person away to protect an agent's turn gets
        #    the priority exactly backwards.
        #  * a send that ARRIVED over the wire (``msg_id`` set: a guest's
        #    forward, or a resequenced outbox entry) has already been
        #    accepted somewhere. Refusing it here would not un-send it; it
        #    would only lose it, and lose it silently.
        deferred: List[dict] = []
        if not external and msg_id is None:
            deferred = self.congested_recipients(mesh, recipients)
            if deferred:
                for entry in deferred:
                    self._record_refusal(mesh, entry["handle"], from_handle)
                # The refused message will not exist after this call, so the
                # one thing that must survive it -- that the sender still
                # means to reach these members -- goes on the sender's own
                # loop ledger, where a re-briefing can hand it back.
                if member is not None and self._is_local(mesh, member):
                    loops.note_refused_send(
                        member.session, mesh.name, deferred, body
                    )
                open_to = [r for r in recipients
                           if r not in {e["handle"] for e in deferred}]
                if not open_to:
                    raise MeshBusy(
                        _busy_notice(deferred), deferred,
                        max((e["retry_after"] for e in deferred), default=0.0),
                    )
                # Some had room. Narrow the ADDRESS rather than the
                # recipient list alone: the log stores the address and
                # delivery re-derives from it (see Mesh.addressed_to), so a
                # ``"*"`` left intact would reach the refused members on the
                # next tick anyway and make the bounce a lie.
                recipients = open_to
                to = open_to
                if norm_sections is not None:
                    norm_sections = {
                        h: sec for h, sec in norm_sections.items()
                        if h in open_to
                    } or None
        msg = {
            "id": msg_id or ("msg-" + uuid.uuid4().hex[:12]),
            "ts": str(ts or "") or utcnow(),
            "from": from_handle,
            "to": to if isinstance(to, str) else list(to),
            "type": intent,
            # (epoch, seq) is the authoritative order. Epoch only moves on an
            # authority handover, so a forced takeover cannot interleave with
            # the old authority's late traffic.
            "epoch": mesh.authority_epoch,
            "seq": mesh.next_seq,
            # The log keeps the full composite; delivery slices per recipient
            # from ``shared`` + ``sections`` (see format_delivery).
            "body": (
                _composite_body(body, norm_sections)
                if norm_sections is not None
                else body
            ),
        }
        if reply_to:
            msg["reply_to"] = str(reply_to)
        if isinstance(ref, dict) and ref:
            msg[REF_KEY] = ref
        if norm_sections is not None:
            msg["shared"] = body
            msg["sections"] = norm_sections
        mesh.next_seq += 1
        self._fold_provisional(mesh, msg["id"])
        mesh.messages.append(msg)
        mesh.seen_ids.add(msg["id"])
        mesh.last_append = time.monotonic()
        self._append_log(mesh, msg)
        self._settle_response_watch(mesh, msg)
        now = time.monotonic()
        if member is not None and self._is_local(mesh, member):
            mesh.activity.setdefault(member.handle, {"anchor": now})[
                "last_sent"
            ] = now
            loops.note_delivered_send(member.session, mesh.name, recipients)
        for handle in recipients:
            rcpt = mesh.members.get(handle)
            if rcpt is not None and self._is_local(mesh, rcpt):
                mesh._first_pending.setdefault(handle, now)
        mesh.wake.set()
        remote = [
            h for h in recipients
            if h in mesh.members and not self._is_local(mesh, mesh.members[h])
        ]
        advisories = []
        if norm_sections is None:
            sep = _separable_notice(body, recipients)
            if sep:
                advisories.append(sep)
        else:
            thin = _preamble_only_notice(body, norm_sections, recipients)
            if thin:
                advisories.append(thin)
        note = type_notice(intent)
        if not note and norm_sections:
            for sec in norm_sections.values():
                if sec.get("type"):
                    note = type_notice(sec["type"])
                    if note:
                        break
        if note:
            advisories.append(note)
        if broadcast:
            advisories.append(broadcast_notice(recipients))
        # A recipient whose terminal is gone is the one advisory the sender
        # cannot work out for itself: delivery accepts the message either
        # way, so 'sent' looks identical whether bob is reading or bob died
        # an hour ago. Said FIRST — it changes what the sender does next,
        # while the others only change how it phrases the next message.
        stranded = self.stranded_recipients(mesh, recipients)
        stranded_note = stranded_notice(stranded)
        if stranded_note:
            advisories.insert(0, stranded_note)
        # Ahead of even the stranded note: a partial send is the one outcome
        # where "sent" is true and incomplete at the same time, and the
        # sender has to resend to the rest or it never arrives.
        if deferred:
            advisories.insert(0, _busy_notice(deferred))
        return {
            **msg,
            "recipients": recipients,
            "queued": False,
            # Recipients this send was refused for — their backlog is at the
            # cap. Not an error here (others took it); the whole-send
            # refusal is MeshBusy. Same shape either way, so a caller marks
            # the rows the same way in both.
            "deferred": deferred,
            # Accepted and queued, but nothing there to read it — see
            # stranded_notice(). Structured as well as prose so a dashboard
            # can mark the row without parsing the sentence.
            "undeliverable": stranded,
            # Remote recipients ride the guest fanout; when the relay is down
            # they are queued (durably, via the guest cursor) until reconnect.
            "queued_remote": remote if not self.relay_connected() else [],
            "remote": remote,
            "batched": norm_sections is not None,
            "expects_reply": expects_reply(intent),
            # Advisory, not an error: an unknown intent invites reply-all,
            # and a body @-addressing several recipients wants to be a batch.
            "notice": " ".join(advisories) if advisories else None,
        }

    async def _send_from_mirror(
        self,
        mesh: Mesh,
        sender: str,
        to: Union[str, List[str]],
        body: str,
        *,
        external: bool,
        type: str,
        reply_to: Optional[str],
        sections: Optional[dict],
        ref: Optional[dict] = None,
    ) -> dict:
        """Forward a mirror-side send to the primary (or queue it durably).

        Validation that can fail fast happens here against the mirror's
        converged roster; the primary re-validates authoritatively. Only a
        *transport* failure queues — an application rejection surfaces.
        """
        member = self.resolve_sender(mesh.name, sender)
        if member is None and not external:
            raise MeshError(
                f"{sender!r} is neither a handle nor a member session in mesh "
                f"{mesh.name!r} (join first: claunch mesh join {mesh.name})"
            )
        if member is not None and not external and member.machine != self.machine:
            raise MeshError(
                f"{sender!r} is not a member on this daemon — send from the "
                f"daemon that owns that session"
            )
        from_handle = member.handle if member is not None else sender
        body = _CTRL_RE.sub("", str(body)).strip()
        recipients = self._resolve_recipients(mesh, from_handle, to)
        if not recipients:
            raise MeshError(self._nobody_to_deliver_to(mesh, from_handle))
        norm_sections = _normalize_sections(sections, recipients, from_handle)
        if norm_sections is None and not body:
            raise MeshError("empty message body")
        intent = str(type or "say").strip().lower() or "say"
        entry: dict = {
            "id": "msg-" + uuid.uuid4().hex[:12],
            "ts": utcnow(),
            "from": from_handle,
            "to": to if isinstance(to, str) else list(to),
            "type": intent,
            "body": body,
        }
        if external:
            entry["external"] = True
        if reply_to:
            entry["reply_to"] = str(reply_to)
        if isinstance(ref, dict) and ref:
            entry[REF_KEY] = ref
        if norm_sections is not None:
            entry["sections"] = norm_sections
        if member is not None and self._is_local(mesh, member):
            now = time.monotonic()
            mesh.activity.setdefault(member.handle, {"anchor": now})[
                "last_sent"
            ] = now
        # Order preservation: while a backlog exists, new sends must line up
        # behind it even if the primary is reachable again.
        if not mesh.outbox and self.peer_transport is not None:
            try:
                result = await self._peer_call_primary(
                    mesh, "/peer/mesh/send", {"message": entry}
                )
            except PeerUnreachable:
                pass  # fall through to the outbox
            else:
                result.setdefault("queued", False)
                # The authority judged liveness for the members IT hosts;
                # ours are remote from there and came back unjudged. Merge
                # our half in, so a sender on a mirror hears about a dead
                # peer in the same room it is standing in.
                self._merge_stranded(mesh, result, recipients)
                return result
        mesh.outbox.append(entry)
        self._persist_outbox(mesh)
        self._flush_upstream_soon(mesh)
        # The authority is down but our peers need not be: push straight to
        # the daemons hosting the recipients so the conversation continues.
        # The outbox still holds the message for sequencing on reconnect.
        if self._fast_targets(mesh, recipients):
            try:
                await self._fast_deliver(mesh, entry)
            except Exception:  # noqa: BLE001 — the send is already queued
                log.exception("mesh %r: fast-path delivery failed", mesh.name)
        direct = list(entry.get("fast_sent") or [])
        result = {
            **entry,
            "queued": True,
            "recipients": [],
            "remote": [],
            "queued_remote": [],
            "undeliverable": [],
            # Shape parity with the sequenced path: nothing was refused here
            # because nothing was judged here — the authority is down, and
            # it is the authority that holds the door.
            "deferred": [],
            "batched": norm_sections is not None,
            "expects_reply": expects_reply(intent),
            "notice": (
                f"queued: authority daemon {mesh.primary!r} is unreachable — "
                "the message will be sequenced on reconnect"
                + (
                    f"; delivered directly to {', '.join(sorted(direct))}"
                    if direct else ""
                )
            ),
        }
        self._merge_stranded(mesh, result, recipients)
        return result

    def _merge_stranded(
        self, mesh: Mesh, result: dict, recipients: Iterable[str]
    ) -> None:
        """Fold this daemon's own liveness judgement into a send ``result``.

        Only ever ADDS: the entries already there were judged by whichever
        daemon hosts those members, and it is the one that can see them.
        The notice goes in front of whatever else the send had to say —
        'the recipient is dead' outranks 'this looks like a batch'.
        """
        mine = self.stranded_recipients(mesh, recipients)
        if not mine:
            result.setdefault("undeliverable", [])
            return
        known = {e.get("handle") for e in (result.get("undeliverable") or [])}
        merged = list(result.get("undeliverable") or [])
        merged += [e for e in mine if e["handle"] not in known]
        result["undeliverable"] = merged
        note = stranded_notice(mine)
        prior = result.get("notice")
        result["notice"] = f"{note} {prior}" if prior else note

    @staticmethod
    def _nobody_to_deliver_to(mesh: Mesh, from_handle: str) -> str:
        """Why a send resolved to nobody — an empty mesh and a fully isolated
        member look identical at the call site and need opposite fixes."""
        others = [h for h in mesh.members if h != from_handle]
        if not others:
            return f"mesh {mesh.name!r} has no other members to deliver to"
        return (
            f"{from_handle!r} is not connected to any of the "
            f"{len(others)} other member(s) of mesh {mesh.name!r} — ask the "
            "session that spawned you to connect you to a peer"
        )

    def stranded_recipients(self, mesh: Mesh, recipients: Iterable[str]) -> List[dict]:
        """Which of ``recipients`` have no terminal left to read a message.

        Only LOCAL members can be answered here: a guest member's liveness is
        its own daemon's to know, and the roster already reports that through
        ``reachability`` (remote-connected / remote-disconnected). Guessing on
        their behalf would put a respawn hint in front of a sender who cannot
        run it.

        Each entry is ``{handle, session, state}`` with ``state`` one of
        ``exited`` (respawnable — the record is kept) or ``missing`` (the
        record itself is gone, so nothing can revive it). Both are states
        :meth:`_deliver_to` already refuses to deliver into; this is the same
        judgement, made where the SENDER can still act on it.
        """
        out: List[dict] = []
        for handle in recipients:
            member = mesh.members.get(handle)
            if member is None or not self._is_local(mesh, member):
                continue
            try:
                session = self.manager.get(member.session)
            except ManagerError:
                out.append(
                    {"handle": handle, "session": member.session, "state": "missing"}
                )
                continue
            if session.exited:
                out.append(
                    {"handle": handle, "session": member.session, "state": "exited"}
                )
        return out

    def _resolve_recipients(
        self,
        mesh: Mesh,
        from_handle: str,
        to: Union[str, List[str]],
        *,
        strict: bool = True,
    ) -> List[str]:
        """Expand and validate ``to``, honouring the member graph.

        ``strict=False`` filters unreachable targets instead of refusing
        them. That is for re-resolving a message the authority has *already*
        accepted (the fast path): its recipients were checked when it was
        admitted, and an edge cut in the meantime must narrow the delivery,
        not raise inside a background worker.

        The graph is applied here rather than at the API edge because every
        send — local, MCP, relayed from a guest, sliced out of a batch —
        funnels through this one call. An ACL with a second entrance is not
        an ACL.

        ``*`` narrows silently to the sender's neighbours (a broadcast means
        "everyone I can reach", and has always excluded the sender itself),
        while a handle named explicitly and unreachable is an error: the
        agent asked for that peer by name and must not be told it was
        delivered.
        """
        if to == "*":
            return [
                h for h in mesh.members
                if h != from_handle and mesh.connected(from_handle, h)
            ]
        targets = [to] if isinstance(to, str) else list(to)
        unknown = [t for t in targets if t not in mesh.members]
        if unknown:
            if not strict:
                targets = [t for t in targets if t not in set(unknown)]
            else:
                raise MeshError(
                    f"unknown recipient(s) in mesh {mesh.name!r}: "
                    f"{', '.join(unknown)}"
                )
        cut = [
            t for t in targets
            if t != from_handle and not mesh.connected(from_handle, t)
        ]
        if cut and not strict:
            return [t for t in targets if t not in set(cut)]
        if cut:
            reachable = ", ".join(mesh.neighbours(from_handle)) or "(nobody)"
            # The refusal stands — the graph is an ACL, and nothing here
            # delivers the message. What changes is that the ask is not thrown
            # away with it: this is the one moment an agent names, unprompted,
            # the exact peer it needs, and it used to be spent on a string
            # telling it to relay through somebody. See :mod:`wire`.
            filed = [self.file_wire_request(mesh, from_handle, t) for t in sorted(cut)]
            notes = "\n".join(
                wire.filed_note(req, mesh.name) for req in filed if req is not None
            )
            raise MeshError(
                f"{from_handle!r} has no connection to {', '.join(sorted(cut))} "
                f"in mesh {mesh.name!r} — it can reach: {reachable}."
                + (f"\n{notes}" if notes else "")
            )
        return targets

    def history(self, name: str, limit: int = 50) -> List[dict]:
        mesh = self.get(name)
        return mesh.messages[-limit:] if limit > 0 else list(mesh.messages)

    def history_annotated(self, name: str, limit: int = 50) -> List[dict]:
        """:meth:`history`, with every message told where it got to.

        Apart from ``history`` rather than folded into it: the annotation
        re-resolves every recipient against the member graph, per message, and
        the readers that only want the log as it was written (the CLI, the
        wire format a peer syncs) should not pay for a view's question.
        """
        mesh = self.get(name)
        start = max(0, len(mesh.messages) - limit) if limit > 0 else 0
        return [
            {**m, **mesh.delivery_of(m, start + i)}
            for i, m in enumerate(mesh.messages[start:])
        ]

    def history_annotated_page(
        self,
        name: str,
        *,
        limit: int = 50,
        offset: int = 0,
        message_filter: str = "all",
    ) -> dict:
        """A bounded, annotated history page for the dashboard.

        ``offset`` counts backwards from the newest matching message.  The
        returned messages retain their chronological order, which keeps the
        mesh log chat-like while allowing its default view to avoid building
        a large historical DOM.

        ``current`` and ``archived`` are based on the current status of local
        session members.  A remote member is current here because its daemon
        owns the corresponding session record.
        """
        mesh = self.get(name)
        if message_filter not in {"all", "current", "archived"}:
            raise MeshError(f"unknown message filter {message_filter!r}")

        archived_handles = set()
        for handle, member in mesh.members.items():
            if not self._is_local(mesh, member):
                continue
            try:
                if self.manager.get(member.session).archived_at:
                    archived_handles.add(handle)
            except ManagerError:
                continue

        def is_archived(message: dict) -> bool:
            # A broadcast names every current member.  Direct messages name
            # their sender and recipients when those names are members; an
            # external sender alone is insufficient to make a message old.
            handles = []
            sender = message.get("from")
            if sender in mesh.members:
                handles.append(sender)
            recipients = message.get("to")
            if recipients == "*":
                handles.extend(mesh.members)
            elif isinstance(recipients, list):
                handles.extend(h for h in recipients if h in mesh.members)
            elif recipients in mesh.members:
                handles.append(recipients)
            return bool(handles) and all(h in archived_handles for h in handles)

        rows = []
        counts = {"all": len(mesh.messages), "current": 0, "archived": 0}
        for index, message in enumerate(mesh.messages):
            category = "archived" if is_archived(message) else "current"
            counts[category] += 1
            if message_filter == "all" or message_filter == category:
                rows.append((index, message))

        total = len(rows)
        end = max(0, total - offset)
        start = max(0, end - limit) if limit else end
        page = rows[start:end]
        return {
            "messages": [
                {**message, **mesh.delivery_of(message, index)}
                for index, message in page
            ],
            "page": {
                "filter": message_filter,
                "limit": limit,
                "offset": offset,
                "total": total,
                "counts": counts,
                "has_newer": offset > 0,
                "has_older": start > 0,
            },
        }

    # ------------------------------------------------------------------ #
    # policy config
    # ------------------------------------------------------------------ #
    def set_policy(self, name: str, patch: dict) -> dict:
        """Apply a partial policy edit (validated deep-merge) and persist.

        Primary-only: the policy engine runs on the mesh's owner, so a
        mirror's copy is read-only (it syncs from the primary).
        """
        mesh = self.get(name)
        if mesh.primary:
            raise MeshError(
                f"mesh {name!r} is a mirror — policy is owned by the primary "
                f"daemon ({mesh.primary}); edit it there"
            )
        try:
            mesh.policy = mesh_policy.merge_policy(mesh.policy, patch)
        except mesh_policy.PolicyError as exc:
            raise MeshError(f"bad policy: {exc}") from None
        self._persist_def(mesh)
        self._flush_guests_soon(mesh)
        return mesh.policy

    # ------------------------------------------------------------------ #
    # roles: the vocabulary this mesh's handles resolve into
    # ------------------------------------------------------------------ #
    def _resolve_role(self, mesh: Mesh, handle: str, role: str) -> str:
        """Settle the role to STORE for a joining member.

        Resolved once, here, and kept as a plain string: a later role-set
        upload never rewrites it (uploads are not retroactive). A member whose
        role the vocabulary later drops simply matches no rule.

        This is also where an ``exclusive`` role is enforced — every join
        funnel (local, guest, establishment) resolves through here on the
        authority, so a second live holder is refused in exactly one place.
        """
        try:
            resolved = mesh.roleset.resolve(handle, role)
        except mesh_roles.RoleError as exc:
            raise MeshError(str(exc)) from None
        holder = self.exclusive_holder(mesh, handle, role)
        if holder is not None:
            raise MeshConflict(
                f"role {resolved!r} is exclusive in mesh {mesh.name!r} and "
                f"{holder.handle!r} already holds it — join under another "
                f"role (a crew of your own makes you a worker that "
                f"integrates upward), or have {holder.handle!r} leave "
                f"first"
            )
        return resolved

    def _resolve_subroles(
        self, mesh: Mesh, primary: str, subroles: Sequence[str]
    ) -> List[str]:
        """Settle the subroles to STORE next to ``primary``.

        Each name goes through the same alias index the primary does, so
        ``--subrole qa`` stores ``reviewer``; an unknown name is refused, not
        dropped, for the reason ``RoleSet.resolve`` gives. The primary is
        never repeated as a subrole, and an ``exclusive`` role is exclusive
        however it is held — a second live ``leader`` is refused whether it
        arrives as a primary or as a subrole.
        """
        out: List[str] = []
        for name in _read_subroles(list(subroles)):
            canon = mesh.roleset.canonical(name)
            if canon is None:
                known = ", ".join(sorted(mesh.roleset.roles))
                raise MeshError(f"unknown subrole {name!r} (known: {known})")
            if canon == primary or canon in out:
                continue
            role_def = mesh.roleset.get(canon)
            if role_def is not None and role_def.exclusive:
                holder = self._live_holder(mesh, canon)
                if holder is not None:
                    raise MeshConflict(
                        f"role {canon!r} is exclusive in mesh {mesh.name!r} "
                        f"and {holder.handle!r} already holds it — it cannot "
                        f"be taken as a subrole either"
                    )
            out.append(canon)
        return out

    def _new_member(
        self, mesh: Mesh, handle: str, session: str, role: str,
        subroles: Sequence[str], *, machine: str = "",
    ) -> Member:
        """A member record with its role AND subroles resolved for this mesh.

        The one constructor every join funnel uses, so a subrole is settled in
        exactly the place the primary is.
        """
        primary = self._resolve_role(mesh, handle, role)
        return Member(
            handle, session, machine=machine, role=primary,
            subroles=self._resolve_subroles(mesh, primary, subroles),
        )

    def exclusive_holder(
        self, mesh: Mesh, handle: str, role: str,
        subroles: Sequence[str] = (),
    ) -> Optional[Member]:
        """The live member blocking ``(handle, role)`` from joining, or None.

        Public so session creation can ask the question BEFORE building
        anything (``onboard.preflight``): a create that would come up outside
        its mesh is refused outright rather than half-succeeding. A role the
        vocabulary cannot resolve answers None — the join itself refuses it
        with the right message, and this check must not shadow that one.
        ``subroles`` are checked the same way: an exclusive role held live by
        somebody blocks a joiner that names it in either position.
        """
        try:
            resolved = mesh.roleset.resolve(handle, role)
        except mesh_roles.RoleError:
            return None
        wanted = [resolved] + [
            c for c in (
                mesh.roleset.canonical(n) for n in _read_subroles(list(subroles))
            )
            if c
        ]
        for name in wanted:
            role_def = mesh.roleset.get(name)
            if role_def is None or not role_def.exclusive:
                continue
            holder = self._live_holder(mesh, name)
            if holder is not None:
                return holder
        return None

    def set_subroles(
        self, name: str, handle: str, *,
        add: Sequence[str] = (), remove: Sequence[str] = (),
        replace: Optional[Sequence[str]] = None,
    ) -> Member:
        """Change a live member's subroles — the one role edit a roster allows.

        The primary role stays what the join settled: it is the member's
        stance and identity, and a member that changes what it IS should
        re-join. A subrole is what it additionally answers for, and that is
        a leader's (or a person's) decision to make after the fact — the
        case this exists for is a leader taking ``reviewer`` so a workflow's
        ``from: [{role: reviewer}]`` finds it. ``replace`` sets the whole
        list; otherwise ``remove`` is applied, then ``add``. Every name is
        resolved through the vocabulary and an exclusive role is refused
        while somebody else holds it live. Membership is the authority's, so
        on a mirror this is refused with the daemon to ask.
        """
        mesh = self.get(name)
        self._require_authority(mesh, "membership")
        member = mesh.members.get(handle)
        if member is None:
            raise MeshError(f"no member {handle!r} in mesh {name!r}")
        if replace is not None:
            wanted = list(replace)
        else:
            drop = set(_read_subroles(list(remove)))
            drop |= {
                c for c in (mesh.roleset.canonical(n) for n in drop) if c
            }
            wanted = [r for r in member.subroles if r not in drop] + list(add)
        # Resolve against a roster that no longer counts this member's own
        # holdings, so keeping a subrole it already has is not refused as a
        # second holder of it.
        before = member.subroles
        member.subroles = []
        try:
            resolved = self._resolve_subroles(mesh, member.role, wanted)
        except MeshError:
            member.subroles = before
            raise
        member.subroles = resolved
        if resolved != before:
            self._roster_changed(mesh)
        return member

    def _live_holder(self, mesh: Mesh, role_name: str) -> Optional[Member]:
        """The member holding ``role_name`` whose session is still alive.

        A LOCAL holder whose session exited or was removed does not block a
        successor — succession is the whole reason exclusivity is checked
        against liveness rather than the roster alone. A REMOTE holder counts
        as alive unconditionally: its daemon is the only witness to its death,
        and refusing is the safe reading of silence. Not retroactive in the
        other direction either: a dead holder KEEPS its role (uploads and
        successions never rewrite members), so respawning it can put two live
        holders on the roster — the flag guards joins, not history.
        """
        for member in mesh.members.values():
            if role_name not in member.roles:
                continue
            if not self._is_local(mesh, member):
                return member
            try:
                session = self.manager.get(member.session)
            except ManagerError:
                continue
            if not session.exited:
                return member
        return None

    def roles_view(self, name: str) -> dict:
        """This mesh's vocabulary, for the API/CLI/web."""
        mesh = self.get(name)
        rs = mesh.roleset
        return {
            "version": mesh.roles_version,
            "custom": mesh.roles_doc is not None,
            # Whether WE are the authority. Not "may you edit this" — a mirror
            # may, its upload is just forwarded — only whether the change is
            # applied here or a hop away.
            "is_authority": not mesh.primary,
            "authority": mesh.authority,
            "default": rs.default,
            "yaml": mesh_roles.to_yaml(mesh.roles_doc or rs.to_doc()),
            "roles": [
                {
                    "name": r.name,
                    "aliases": list(r.aliases),
                    "stall_watch": r.stall_watch,
                    "exclusive": r.exclusive,
                    "task_poll": r.task_poll,
                    "cflow_reminder": r.cflow_reminder,
                    "stance": r.stance,
                    # How many members currently hold it — the roster is the
                    # only place the vocabulary meets reality, and a role
                    # nobody holds is worth seeing as such.
                    "members": sorted(
                        h for h, m in mesh.members.items() if r.name in m.roles
                    ),
                }
                for r in (rs.roles[n] for n in sorted(rs.roles))
            ],
            # Roles held by a member but no longer in the vocabulary — the
            # visible face of "uploads are not retroactive".
            "orphans": sorted(
                {
                    r for m in mesh.members.values() for r in m.roles
                    if not rs.get(r)
                }
            ),
        }

    async def set_roles(self, name: str, doc) -> dict:
        """Upload (or clear, with ``doc=None``) this mesh's role-set override.

        The authority owns the vocabulary — one mesh, one set of role names,
        or two daemons would read the same handle differently. A mirror's
        upload is therefore *forwarded* rather than refused: the dashboard a
        user happens to have open should not have to be the authority's.
        """
        mesh = self.get(name)
        parsed = None
        if doc is not None:
            try:
                parsed = mesh_roles.parse(doc)
                mesh_roles.resolve(parsed)  # must resolve before we adopt it
            except mesh_roles.RoleError as exc:
                raise MeshError(f"bad role set: {exc}") from None
        if mesh.primary:
            payload = await self._peer_call_primary(
                mesh, "/peer/mesh/roles", {"roles": parsed}
            )
            mesh.set_roles_doc(parsed, version=payload.get("version"))
            self._persist_def(mesh)
            return self.roles_view(name)
        self._adopt_roles(mesh, parsed)
        return self.roles_view(name)

    def _adopt_roles(self, mesh: Mesh, parsed: Optional[dict]) -> None:
        """Authority side: take the new vocabulary and push it to the guests."""
        if not mesh.set_roles_doc(parsed):
            return
        self._persist_def(mesh)
        self._flush_guests_soon(mesh)
        log.info(
            "mesh %r: role set %s (version %d): %s",
            mesh.name, "replaced" if parsed else "reset to the default",
            mesh.roles_version, ", ".join(sorted(mesh.roleset.roles)),
        )

    def peer_roles_accept(
        self, name: str, machine: str, token: str, doc
    ) -> dict:
        """A peer asks us, the authority, to change the mesh's role set."""
        mesh = self._inbound(name, machine, token)
        self._require_authority(mesh, "the role set")
        self._check_link_token(mesh, machine, token)
        parsed = None
        if doc is not None:
            try:
                parsed = mesh_roles.parse(doc)
                mesh_roles.resolve(parsed)
            except mesh_roles.RoleError as exc:
                raise MeshError(f"bad role set: {exc}") from None
        self._adopt_roles(mesh, parsed)
        return {"version": mesh.roles_version}

    # ------------------------------------------------------------------ #
    # federation v2: primary/mirror. The primary owns roster, log, policy
    # and invites; guests hold a synced mirror and forward member requests.
    # ------------------------------------------------------------------ #
    # ------------------------------------------------------------------ #
    # topology: rank order and per-link state
    # ------------------------------------------------------------------ #
    async def reorder_peers(
        self, name: str, order: List[str], *, force: bool = False
    ) -> dict:
        """Rewrite the rank list — the one way authority moves.

        Only the current authority may reorder, so two daemons can never
        promote themselves at once; ``force`` is the escape hatch for an
        authority that is gone for good, and it bumps ``authority_epoch`` so
        the old one's late traffic is re-sequenced rather than interleaved.
        """
        mesh = self.get(name)
        order = [str(m).strip() for m in order if str(m).strip()]
        if sorted(order) != sorted(mesh.peers):
            missing = sorted(set(mesh.peers) - set(order))
            extra = sorted(set(order) - set(mesh.peers))
            raise MeshError(
                "the new order must list exactly the mesh's peers"
                + (f" (missing: {', '.join(missing)})" if missing else "")
                + (f" (unknown: {', '.join(extra)})" if extra else "")
            )
        if not order:
            raise MeshError(f"mesh {name!r} has no peers to order")
        if mesh.primary and not force:
            raise MeshError(
                f"rank order is the authority's to set ({mesh.primary}) — "
                "reorder there, or force a takeover if it is gone for good"
            )
        was, now = mesh.peers[0], order[0]
        if force and mesh.primary and now != mesh.me:
            raise MeshError(
                "a forced takeover has to put this daemon at rank 0 — it is "
                "the only order the other peers can be told about from here"
            )
        # Absolutise against the OUTGOING rank order, which is why this runs
        # before the assignment below and must not be moved past it: the
        # blanks belong to the authority losing rank 0, not to whoever is
        # taking it. In a forced takeover that is somebody else's daemon.
        self._absolutize_roster(mesh)
        before = (list(mesh.peers), mesh.authority_epoch, mesh.next_seq)
        mesh.peers = order
        if now != was:
            mesh.authority_epoch += 1
            self._raise_seq_floor(mesh)
        self._ensure_pair_links(mesh)
        self._roster_changed(mesh)
        if now == was:
            self._flush_guests_soon(mesh)
            return {
                "peers": list(mesh.peers),
                "authority": mesh.authority,
                "epoch": mesh.authority_epoch,
                "handover": False,
            }
        # We have just demoted ourselves, so the ordinary authority fanout is
        # closed to us: hand the new order over explicitly, while we still
        # hold every peer's credentials. The successor MUST get it — if it
        # does not, nobody would be sequencing, so roll the whole thing back.
        if now != mesh.me and not await self._flush_guest(
            mesh, now, force=True, urgent=True
        ):
            mesh.peers, mesh.authority_epoch, mesh.next_seq = before
            self._ensure_pair_links(mesh)
            self._roster_changed(mesh)
            raise MeshError(
                f"{now!r} could not be told it is the new authority — it is "
                "unreachable, so the rank order was left unchanged"
            )
        log.info(
            "mesh %r: authority moved %r -> %r (epoch %d)",
            mesh.name, was, now, mesh.authority_epoch,
        )
        await self._handover_flush(mesh, skip=now)
        return {
            "peers": list(mesh.peers),
            "authority": mesh.authority,
            "epoch": mesh.authority_epoch,
            "handover": now != was,
        }

    async def _handover_flush(self, mesh: Mesh, *, skip: str = "") -> None:
        """One last push from the outgoing authority, carrying the new order.

        Only the successor's copy is load-bearing (the caller sends that one
        and refuses the handover if it fails). For the rest an unreachable
        peer is not fatal: it still believes we are rank 0, and the new
        authority's own syncs correct it — the credential pair it presents
        was brokered by the same authority either way.
        """
        if self.peer_transport is None:
            return
        for machine in list(mesh.links):
            if machine == skip:
                continue
            try:
                await self._flush_guest(mesh, machine, force=True, urgent=True)
            except Exception as exc:  # noqa: BLE001 — best-effort
                log.warning(
                    "mesh %r: could not hand the new order to %r: %s",
                    mesh.name, machine, exc,
                )

    async def set_link(
        self, name: str, a: str, b: str, *, enabled: bool
    ) -> dict:
        """Cut or restore the direct edge between two peers.

        Who may do this: the authority may edit any edge, and a peer may edit
        the edges it *terminates*. An edge is duplex and both ends have equal
        standing on it, so either one may sever their own connection — but
        an edge between two other daemons is not yours to touch, and that
        request goes nowhere. A peer's edit is forwarded to the authority,
        which owns the table and fans the result back out.

        A cut edge only loses its *fast path* — the authority's fanout still
        reaches both ends — so cutting an edge that touches the authority
        would orphan a daemon instead of degrading it, and is refused.
        """
        mesh = self.get(name)
        self._validate_edge(mesh, a, b)
        if mesh.primary:
            if mesh.me not in (a, b):
                raise MeshError(
                    f"{a!r} <-> {b!r} is an edge between two other daemons — "
                    f"only the authority ({mesh.primary}) can edit it"
                )
            await self._peer_call_primary(
                mesh, "/peer/mesh/link", {"a": a, "b": b, "enabled": enabled}
            )
            # Optimistic: the authority accepted, so show it now rather than
            # at the next sync — which re-sends the whole table anyway.
            self._mark_edge(mesh, a, b, enabled)
            return {"a": a, "b": b, "enabled": bool(enabled)}
        self._mark_edge(mesh, a, b, enabled)
        self._roster_changed(mesh)  # ships the new state on the next sync
        self._flush_guests_soon(mesh)
        return {"a": a, "b": b, "enabled": bool(enabled)}

    def _validate_edge(self, mesh: Mesh, a: str, b: str) -> None:
        """Shape checks every caller shares — local, peer-forwarded or HTTP."""
        for machine in (a, b):
            if machine not in mesh.peers:
                raise MeshError(f"{machine!r} is not a peer of mesh {mesh.name!r}")
        if a == b:
            raise MeshError("an edge needs two different peers")
        if mesh.authority in (a, b):
            raise MeshError(
                f"the edge to the authority ({mesh.authority}) carries the "
                "sequenced log — revoke the peer instead of cutting it"
            )

    def _mark_edge(self, mesh: Mesh, a: str, b: str, enabled: bool) -> None:
        """Record an edge's cut state, and mirror it onto our own credential
        when we are one of its ends — ``linked()`` reads that, not the table."""
        key = self._pair_key(a, b)
        mesh.edges[key] = bool(enabled)
        other = b if a == mesh.me else (a if b == mesh.me else "")
        if other and other in mesh.links:
            mesh.links[other]["enabled"] = bool(enabled)

    def peer_link_accept(
        self, name: str, machine: str, token: str, a: str, b: str, enabled: bool
    ) -> dict:
        """A peer asks us, the authority, to cut or restore its own edge."""
        mesh = self._inbound(name, machine, token)
        self._require_authority(mesh, "link state")
        self._check_link_token(mesh, machine, token)
        if machine not in (a, b):
            raise MeshError(
                f"{machine!r} is not an end of {a!r} <-> {b!r} — a peer may "
                "only edit the edges it terminates"
            )
        self._validate_edge(mesh, a, b)
        self._mark_edge(mesh, a, b, enabled)
        self._roster_changed(mesh)
        self._flush_guests_soon(mesh)
        log.info(
            "mesh %r: %s %s the edge %s <-> %s",
            mesh.name, machine, "restored" if enabled else "cut", a, b,
        )
        return {"a": a, "b": b, "enabled": bool(enabled)}

    # ------------------------------------------------------------------ #
    # the member graph
    # ------------------------------------------------------------------ #
    async def set_member_link(
        self,
        name: str,
        a: str,
        b: str,
        *,
        enabled: bool,
        actor: str = "",
    ) -> dict:
        """Connect or disconnect two members of ``name``.

        Ownership follows the roster, not the endpoints: the authority owns
        membership, so it owns who may speak to whom, and a guest forwards
        the edit up (``/peer/mesh/member-link``) instead of applying it
        locally. That is stricter than the machine graph, where either end of
        an edge may cut it — but the endpoints there are *daemons*, mutually
        consenting adults with their own operators, whereas the endpoints
        here are agents, and a member that could cut its own edges could quit
        the supervision it was spawned under.

        ``actor`` is the session making the request (empty for a human
        acting through the CLI or dashboard, who is not restricted). An agent
        may only edit an edge that touches a session it commands — the
        children it spawned, and their descendants. So a lead wires its own
        workers together, and a worker cannot wire itself to anybody.

        Connecting a pair that had a standing wire request also **grants** it
        and tells the requester (see :meth:`_grant_wire_request`). There is no
        separate approve verb: the request asks for an edge, so the edge is
        the answer, and a second state saying "approved" could only ever
        disagree with the graph.
        """
        mesh = self.get(name)
        self._validate_member_edge(mesh, a, b)
        if actor:
            self._require_member_authority(mesh, actor, a, b)
        if mesh.primary:
            await self._peer_call_primary(
                mesh, "/peer/mesh/member-link", {"a": a, "b": b, "enabled": enabled}
            )
            # Optimistic, as with a machine edge: the authority accepted, and
            # the next sync re-sends the whole table anyway.
            self._mark_member_edge(mesh, a, b, enabled)
            granted = self._grant_wire_request(mesh, a, b, actor) if enabled else None
            self._persist_def(mesh)
            return {
                "a": a, "b": b, "enabled": bool(enabled),
                **({"granted": granted} if granted else {}),
            }
        self._mark_member_edge(mesh, a, b, enabled)
        granted = self._grant_wire_request(mesh, a, b, actor) if enabled else None
        self._persist_def(mesh)
        self._roster_changed(mesh)
        self._flush_guests_soon(mesh)
        log.info(
            "mesh %r: %s the member edge %s <-> %s",
            mesh.name, "connected" if enabled else "disconnected", a, b,
        )
        return {
            "a": a, "b": b, "enabled": bool(enabled),
            **({"granted": granted} if granted else {}),
        }

    # ------------------------------------------------------------------ #
    # wire requests: the refusal, kept instead of spent
    #
    # Local to this daemon and deliberately not federated. A refusal happens
    # on the SENDER's daemon, the lineage that decides who may grant it is
    # only knowable there, and the approver it names is a session with a
    # terminal there. Shipping the table to the authority would move a record
    # away from every party that can act on it. See :mod:`wire`.
    # ------------------------------------------------------------------ #
    def file_wire_request(
        self, mesh: Mesh, requester: str, target: str
    ) -> Optional["wire.WireRequest"]:
        """Record that ``requester`` needs ``target``, and ask who can grant it.

        Called from the one place a named send is refused for want of an edge
        (:meth:`_resolve_recipients`). Returns the request as it now stands —
        the refusal message is built from it — or ``None`` when there is
        nothing to record (an unknown handle, or a self-send, both of which
        the caller has already rejected on their own terms).

        Never raises. This runs *inside* an exception path that is about to
        raise something better, and a bookkeeping failure that replaced the
        real refusal with a stack trace would take a legible answer away from
        the agent standing there.
        """
        try:
            if requester == target or target not in mesh.members:
                return None
            key = wire.pair_key(requester, target)
            now = wire.now()
            req = mesh.wire_requests.get(key)
            if req is None:
                req = wire.WireRequest(
                    a=requester, b=target, by=requester, at=now
                )
                mesh.wire_requests[key] = req
            else:
                # ``by`` is never re-pointed: the requester of record is
                # whoever asked FIRST. A pair where both ends try to reach
                # each other is one need, not two, and crediting the latest
                # asker would lose who has been waiting — and would send the
                # grant notice to the wrong one of them.
                req.count += 1
            silent = req.silent(now)
            if not silent:
                handle, kind = self._wire_approver(mesh, req)
                req.approver = handle
                if handle:
                    req.notified_at = now
                    self._notify_wire_request_soon(mesh, req, kind)
            wire.trim(mesh.wire_requests)
            self._persist_def(mesh)
            return req
        except Exception as exc:  # noqa: BLE001 — see the docstring
            log.warning("mesh %r: cannot file a wire request: %s", mesh.name, exc)
            return None

    def _wire_approver(self, mesh: Mesh, req: "wire.WireRequest") -> tuple:
        """``(handle, kind)`` for the session that may open this pair.

        Resolves both ends to sessions before asking, because authority is a
        property of the session tree; a handle is only what a session is
        called inside one mesh.
        """
        by = mesh.members.get(req.by)
        other = mesh.members.get(req.other)
        if by is None or other is None or not self._is_local(mesh, by):
            return "", ""

        def handle_of(session: str) -> str:
            member = self.member_for_session(mesh, session)
            return member.handle if member is not None else ""

        return wire.approver(
            by.session,
            other.session,
            ancestors_of=self.manager.ancestors,
            handle_of=handle_of,
        )

    def _notify_wire_request_soon(
        self, mesh: Mesh, req: "wire.WireRequest", kind: str
    ) -> None:
        """Schedule the approver's notice, or send it inline with no loop.

        Scheduled rather than awaited because the caller is a synchronous
        resolver inside a raise path: the refusal must reach the sender now,
        not after a message round-trip to somebody else.
        """
        body = wire.notice_body(req, mesh.name, kind=kind)
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            self._say_as_policy(mesh, [req.approver], body, type="decide")
            return
        asyncio.ensure_future(
            self._notify_wire_request(mesh, req.approver, body)
        )

    async def _notify_wire_request(
        self, mesh: Mesh, approver: str, body: str
    ) -> None:
        self._say_as_policy(mesh, [approver], body, type="decide")

    def _say_as_policy(
        self, mesh: Mesh, to: List[str], body: str, *, type: str = "fyi"
    ) -> None:
        """One message from the daemon's own handle, failure logged not raised.

        The same voice a stall warning speaks in (:meth:`_report_stranded`),
        and for the same reason it is external: the policy engine has no
        terminal in this mesh, so nothing can be owed back to it.
        """
        targets = [h for h in to if h and h in mesh.members]
        if not targets:
            return
        try:
            self._send_core(
                mesh, mesh_policy.POLICY_SENDER, targets, body,
                external=True, type=type,
            )
            self._flush_guests_soon(mesh)
        except MeshError as exc:
            log.debug("mesh %r: policy message failed: %s", mesh.name, exc)

    def _grant_wire_request(
        self, mesh: Mesh, a: str, b: str, actor: str
    ) -> Optional[dict]:
        """Settle an open request because this pair just got connected.

        Granting has no verb of its own: connecting IS the grant. That is not
        a shortcut, it is the honest model — the request asks for an edge, and
        the edge existing is the whole of what was asked for. A separate
        "approve" that then had to open the edge would be two states to keep
        in step and one of them would eventually be wrong.

        Returns a small record for the caller's result, or ``None`` when this
        pair had nothing pending — which is the ordinary case for a human or
        an agent wiring two members nobody asked about.
        """
        req = mesh.wire_requests.get(wire.pair_key(a, b))
        if req is None or req.state != wire.OPEN:
            return None
        req.state = wire.GRANTED
        req.decided_by = actor or "an operator"
        req.decided_at = wire.now()
        self._say_as_policy(mesh, [req.by], wire.granted_note(req, mesh.name))
        log.info(
            "mesh %r: wire request %s <-> %s granted by %s after %d ask(s)",
            mesh.name, req.a, req.b, req.decided_by, req.count,
        )
        return {"by": req.by, "other": req.other, "asks": req.count}

    def decline_wire_request(
        self, name: str, a: str, b: str, *, actor: str = "", reason: str = ""
    ) -> dict:
        """Answer a wire request with no, once and for good.

        A decline is the half of this that makes the whole thing safe to
        leave running. Without it the only answers are "connect" and
        "silence", and silence is what a refused agent retries against
        forever. Recorded so the refusal itself can carry the answer: the
        next time the requester tries, its own error message tells it that
        this was decided and by whom, without another message crossing the
        mesh.

        Subject to the same authority as connecting (an agent edits only
        edges touching a session it commands) — saying no to a channel is as
        much a decision about that pair as saying yes.
        """
        mesh = self.get(name)
        self._validate_member_edge(mesh, a, b)
        if actor:
            self._require_member_authority(mesh, actor, a, b)
        req = mesh.wire_requests.get(wire.pair_key(a, b))
        if req is None:
            raise MeshError(
                f"no wire request for {a!r} <-> {b!r} in mesh {name!r} — "
                f"`claunch mesh wire-requests {name}` lists the open ones"
            )
        if req.state != wire.OPEN:
            return {"already": req.state, **req.to_dict()}
        req.state = wire.DECLINED
        req.decided_by = actor or "an operator"
        req.decided_at = wire.now()
        req.reason = str(reason or "").strip()
        self._say_as_policy(mesh, [req.by], wire.declined_note(req, mesh.name))
        self._persist_def(mesh)
        log.info(
            "mesh %r: wire request %s <-> %s declined by %s (%s)",
            mesh.name, req.a, req.b, req.decided_by, req.reason or "no reason given",
        )
        return req.to_dict()

    def wire_request_rows(self, name: str, *, state: str = "") -> List[dict]:
        """The mesh's wire requests, open ones first then most recent.

        ``state`` filters to one of ``open``/``granted``/``declined``. The
        rows carry the approver each was routed to, so a reader can tell "no
        one has answered" from "no one was asked" — different problems with
        different fixes, and they look identical in a bare count.
        """
        mesh = self.get(name)
        rows = [
            r.to_dict() for r in mesh.wire_requests.values()
            if not state or r.state == state
        ]
        rows.sort(key=lambda r: (r["state"] != wire.OPEN, -float(r["at"] or 0.0)))
        return rows

    async def link_lineage(self, child: str, parent: str) -> List[dict]:
        """Open the parent edge a re-parented session now needs, in every mesh
        both sessions are members of on this daemon.

        A spawn opens this edge at the join (:meth:`_wire_member`), and for
        the same reason: a child that cannot reach its parent cannot report.
        A session moved under a new parent after the fact needs the same edge
        and has no join to get it from, so the re-parent route asks here.
        Only *opening* is done — the edge to the former parent stays, because
        the move changed who commands the child, not who may hear from it;
        cutting that one is the mover's call (``disconnect``).

        Returns the edges opened, ``{mesh, a, b}`` each; an edge already open
        is not reported, so a repeated move says nothing new.
        """
        opened: List[dict] = []
        for entry in self.meshes_for_session(child):
            mesh = self.get(entry["mesh"])
            above = next(
                (
                    h for h, m in mesh.members.items()
                    if m.session == parent and self._is_local(mesh, m)
                ),
                "",
            )
            mine = str(entry["handle"])
            if not above or above == mine:
                continue
            if mesh.member_edges.get(Mesh.member_key(mine, above)):
                continue
            await self.set_member_link(mesh.name, mine, above, enabled=True)
            opened.append({"mesh": mesh.name, "a": mine, "b": above})
        return opened

    async def rewire_members(
        self, name: str, *, actor: str = ""
    ) -> List[dict]:
        """Apply the mesh's current ``auto_link`` rules to the members already here.

        :meth:`_wire_member` runs at a join and stores what it decided, so a
        rule added afterwards reaches nobody who was already in the room. That
        is the right *default* — a member's wiring must not change under it
        because somebody edited a document — and the wrong *only option*: a
        mesh that adopts a rule, or a daemon that ships a new packaged one,
        would otherwise have to hand-wire the fleet it already has, which is
        the manual act the rules exist to remove.

        So this is the same evaluation in explicit form. Three properties
        make it safe to hand an operator:

        * **It only opens.** Nothing is ever cut. A run of this cannot take
          reach away from a member that has it.
        * **A recorded edge is never touched.** Any pair somebody decided —
          a join's wiring, an agent connecting its workers, a human cutting a
          link — is skipped, whichever way it was decided. So a deliberate
          ``disconnect`` survives every future rule and every rerun of this;
          the rules propose wiring, they never overrule a person.
        * **It is idempotent.** A pair already reachable is skipped too
          (an unwired legacy member defaults to open), so a second run
          reports nothing and writes nothing.

        ``actor``, when a caller names one, confines the sweep to that
        session's subtree: every candidate edge is put through the same
        :meth:`_require_member_authority` an explicit edit would face, and a
        pair the actor does not command is skipped rather than refused — a
        sweep names no pair, so there is nothing there to reject. An agent
        therefore gets its own fleet wired ahead of schedule and nothing
        else, which is the rule the D-axis tests state for every other edit.

        With no ``actor`` the whole graph is in scope, and it is worth being
        exact about what holds that. ``actor`` is *declared* by the caller
        here exactly as it is on :meth:`set_member_link`, so omitting it is
        not a thing this layer can detect. Nor is there anywhere it is
        filled in today: the route's only caller is ``claunch mesh rewire``,
        which sends none, there is no MCP tool for this sweep, and the
        dashboard does not call it. So the parameter currently narrows
        nobody. It is here because the check belongs at the door before a
        caller that names one arrives, not after.

        Which means the guarantee this operation carries is not who called
        it, and never was: it is the three properties above. Even at full
        scope it opens only edges the mesh's own rules already sanction, and
        it overrules nobody. There is no reach here that the next join would
        not have produced by itself.

        Returns the edges opened, ``{a, b}`` each, in handle order.
        """
        mesh = self.get(name)
        self._require_authority(mesh, "the member graph")
        facts = self._link_facts(mesh, dict(self._lineage_map(mesh)))
        auto = mesh.roleset.auto_link
        handles = sorted(mesh.members)
        opened: List[dict] = []
        for i, a in enumerate(handles):
            for b in handles[i + 1:]:
                if Mesh.member_key(a, b) in mesh.member_edges:
                    continue  # somebody decided this pair; that decision stands
                if mesh.connected(a, b):
                    continue  # already reachable — writing it down says nothing
                if not auto.decide(facts[a], facts[b]):
                    continue
                if actor:
                    try:
                        self._require_member_authority(mesh, actor, a, b)
                    except MeshError:
                        continue  # not this caller's pair to hurry along
                await self.set_member_link(name, a, b, enabled=True, actor=actor)
                opened.append({"a": a, "b": b})
        log.info(
            "mesh %r: rewire opened %d edge(s): %s",
            mesh.name,
            len(opened),
            ", ".join(f"{e['a']}<->{e['b']}" for e in opened) or "none",
        )
        return opened

    async def isolate_member(
        self, name: str, handle: str, *, keep: Iterable[str] = ()
    ) -> List[str]:
        """Cut ``handle`` off from every member except ``keep``.

        The operator's blunt instrument (see :meth:`Mesh.isolate`), not the
        spawn seed it used to be. Applied one edge at a time through
        :meth:`set_member_link` so a mirror's cuts reach the authority by the
        same forwarding path a hand edit uses — a bulk shortcut here would be
        a second way for the graph to change, and the one that skipped the
        authority.
        """
        mesh = self.get(name)
        if handle not in mesh.members:
            raise MeshError(f"no member {handle!r} in mesh {name!r}")
        kept = set(keep) | {handle}
        cut = []
        for other in sorted(mesh.members):
            if other in kept:
                continue
            await self.set_member_link(name, handle, other, enabled=False)
            cut.append(other)
        return cut

    # ------------------------------------------------------------------ #
    # wiring: what a join connects
    # ------------------------------------------------------------------ #
    def _link_facts(
        self, mesh: Mesh, lineage: Dict[str, str]
    ) -> Dict[str, mesh_roles.LinkFacts]:
        """Every member's (role, tier, root) — all a rule may ask about.

        Derived from the lineage rather than stored on the member, and that is
        safe *because* it is used once: a join reads these, records edges, and
        never consults them again. A parent that exits later re-roots its child
        in the drawn tree, but cannot re-wire a member already wired.
        """
        facts: Dict[str, mesh_roles.LinkFacts] = {}
        for handle, member in mesh.members.items():
            tier, root, seen = 0, handle, {handle}
            while tier < mesh_roles.MAX_TIER:
                parent = lineage.get(root)
                # `not in mesh.members` also stops the walk at a parent that
                # never enrolled; `in seen` stops a hand-edited cycle.
                if not parent or parent in seen or parent not in mesh.members:
                    break
                seen.add(parent)
                root, tier = parent, tier + 1
            facts[handle] = mesh_roles.LinkFacts(
                role=member.role, tier=tier, root=root,
                roles=tuple(member.roles),
            )
        return facts

    def parent_handle_for(self, mesh: Mesh, session: str) -> str:
        """The handle a joining local session would hang off, before it joins.

        The same "nearest *enrolled* ancestor" rule :meth:`_local_lineage`
        applies to members already in the roster — asked one join earlier,
        because the wiring has to know the parent to connect the member to it.
        """
        by_session = {
            m.session: h for h, m in mesh.members.items()
            if self._is_local(mesh, m) and m.session
        }
        for name in self.manager.ancestors(session):
            handle = by_session.get(name)
            if handle:
                return handle
        return ""

    def _wire_member(self, mesh: Mesh, member: Member, parent: str) -> dict:
        """Decide and record the edges a new member starts with. Authority only.

        Two sources, in this order:

        * **every parent edge this member is an end of**, unconditionally —
          the one to its parent, and one to each member already here that is a
          child of *it*. Both directions, because a member can arrive at
          either end of a spawn edge: a parent that left and rejoined would
          otherwise come back unable to reach the children still working for
          it (its edges went with it — see ``prune_member_edges``). A child
          that cannot reach its parent cannot report, and the reply command
          its briefing hands it fails, so this is the join's own doing and no
          ``auto_link`` document can withhold it.
        * **the mesh's rules**, evaluated once per existing member.

        Only *connections* are written. The member is marked ``wired``, which
        makes every pair nobody wrote down closed for it — so isolating a child
        costs one edge to its parent instead of a cut against every member who
        happened to be in the room, and stays isolated from members who arrive
        later without any standing rule to keep re-applying.

        Written straight into the table rather than through
        :meth:`set_member_link`: both callers are already the authority (a
        guest's join is forwarded here first), so the forwarding that method
        exists for would be a round trip to ourselves, once per member.
        """
        lineage = dict(self._lineage_map(mesh))
        if parent and parent in mesh.members:
            lineage[member.handle] = parent
        else:
            parent = ""
        facts = self._link_facts(mesh, lineage)
        mine = facts[member.handle]
        auto = mesh.roleset.auto_link
        opened = []
        for other in sorted(mesh.members):
            if other == member.handle:
                continue
            kin = other == parent or lineage.get(other) == member.handle
            if kin or auto.decide(mine, facts[other]):
                mesh.member_edges[Mesh.member_key(member.handle, other)] = True
                opened.append(other)
        member.wired = True
        log.info(
            "mesh %r: wired %r (role %s, tier %d) to %s",
            mesh.name, member.handle, mine.role, mine.tier,
            ", ".join(opened) or "nobody",
        )
        return {"parent": parent, "connected_to": opened}

    def peer_member_link_accept(
        self, name: str, machine: str, token: str, a: str, b: str, enabled: bool
    ) -> dict:
        """Authority side: apply a member-edge edit forwarded by a peer."""
        mesh = self._inbound(name, machine, token)
        self._check_link_token(mesh, machine, token)
        self._require_authority(mesh, "the member graph")
        self._validate_member_edge(mesh, a, b)
        self._mark_member_edge(mesh, a, b, enabled)
        self._persist_def(mesh)
        self._roster_changed(mesh)
        self._flush_guests_soon(mesh)
        log.info(
            "mesh %r: %s %s the member edge %s <-> %s",
            mesh.name, machine, "connected" if enabled else "disconnected", a, b,
        )
        return {"a": a, "b": b, "enabled": bool(enabled)}

    def _mark_member_edge(
        self, mesh: Mesh, a: str, b: str, enabled: bool
    ) -> None:
        """Record an edge's state and settle any debt it just made undischargeable.

        ``owed`` is recomputed from the log through :meth:`Mesh.addressed_to`,
        so it drops a debt the moment the edge carrying it is cut. The
        heartbeat is not recomputed — ``last_asked`` was stamped at delivery
        and simply stays — so without this the two would disagree: the
        dashboard would show nothing owed while the policy engine kept
        nudging the member to answer. That nudge is worse than noise, because
        a member that obeyed it would have its reply **refused** by the very
        graph that cut the edge.

        Only a member that now owes nothing at all is settled; one with other
        outstanding mail is still legitimately being chased.
        """
        mesh.member_edges[Mesh.member_key(a, b)] = bool(enabled)
        if enabled:
            return
        for handle in (a, b):
            st = mesh.activity.get(handle)
            if st and st.get("last_asked") and not mesh.owed(handle):
                st["last_asked"] = 0.0

    def _validate_member_edge(self, mesh: Mesh, a: str, b: str) -> None:
        for handle in (a, b):
            if handle not in mesh.members:
                raise MeshError(
                    f"{handle!r} is not a member of mesh {mesh.name!r}"
                )
        if a == b:
            raise MeshError("a member edge needs two different members")

    def _require_member_authority(
        self, mesh: Mesh, actor: str, a: str, b: str
    ) -> None:
        """An agent may only rewire an edge that touches a session it commands.

        Resolved through the *session* behind each handle, because the
        hierarchy is a property of sessions and a handle is only a name a
        session wears inside one mesh. A remote member is never commandable
        from here: its session lives on another daemon, whose tree this one
        does not know.
        """
        actor_member = self.resolve_sender(mesh.name, actor)
        actor_handle = actor_member.handle if actor_member else actor
        owned = []
        for handle in (a, b):
            member = mesh.members.get(handle)
            if member is None or not self._is_local(mesh, member):
                continue
            if self.manager.commands(actor, member.session):
                owned.append(handle)
        if not owned:
            raise MeshError(
                f"{actor_handle!r} may not rewire {a!r} <-> {b!r}: an agent "
                "edits only the edges touching a session it spawned (or a "
                "descendant of one). Ask the session that spawned you, or an "
                "operator (claunch mesh connect/disconnect)."
            )

    def _require_machine(self) -> str:
        if not self.machine:
            raise MeshError(
                "cross-machine mesh needs a relay identity — configure the "
                "relay uplink first (claunch daemon relay url/name/token)"
            )
        return self.machine

    def _require_authority(self, mesh: Mesh, what: str) -> None:
        if mesh.primary:
            raise MeshError(
                f"mesh {mesh.name!r} is a mirror — {what} is owned by the "
                f"authority daemon ({mesh.primary}, rank 0)"
            )

    async def _peer_call(self, mesh: Mesh, machine: str, path: str, body: dict):
        """One authenticated request across the link to ``machine``.

        Every edge is duplex and symmetric in shape, so the same call serves
        a peer talking *up* to the authority and one talking *sideways* to
        another peer.
        """
        if self.peer_transport is None:
            raise PeerUnreachable(
                f"relay uplink is not running — cannot reach {machine!r}"
            )
        link = mesh.links.get(machine) or {}
        return await self.peer_transport(
            machine,
            path,
            {
                "mesh": mesh.wire_name,
                "machine": self._require_machine(),
                "token": str(link.get("token_out") or ""),
                **body,
            },
        )

    async def _peer_call_primary(self, mesh: Mesh, path: str, body: dict) -> dict:
        """One authenticated request from this peer up to the authority."""
        return await self._peer_call(mesh, mesh.authority, path, body)

    def invite(self, name: str) -> dict:
        """Mint a pre-approval invite ticket (primary-only).

        A ticket lets ``mesh join name@machine --code X`` skip the pending
        queue — the unattended/automation path. Tickets are single-use and
        expire after ``invite_ttl`` seconds.
        """
        mesh = self.get(name)
        self._require_authority(mesh, "invite minting")
        machine = self._require_machine()
        token = secrets.token_urlsafe(18)
        mesh.invites[token] = utcnow()
        self._persist_def(mesh)
        code = base64.urlsafe_b64encode(
            json.dumps(
                {"v": 2, "mesh": mesh.wire_name, "machine": machine, "token": token}
            ).encode("utf-8")
        ).decode("ascii")
        return {
            "code": code,
            "mesh": mesh.name,
            "machine": machine,
            "expires_in": self.invite_ttl,
        }

    def invite_list(self, name: str) -> List[dict]:
        """Outstanding (unredeemed, unexpired) tickets, oldest first."""
        mesh = self.get(name)
        self._require_authority(mesh, "invite minting")
        self._expire_invites(mesh)
        return [
            {
                "prefix": token[:8],
                "created_at": created,
                "expires_in": max(
                    0.0, self.invite_ttl - self._invite_age(created)
                ),
            }
            for token, created in sorted(
                mesh.invites.items(), key=lambda kv: kv[1]
            )
        ]

    def invite_revoke(self, name: str, prefix: str) -> int:
        """Revoke every outstanding ticket whose token starts with ``prefix``."""
        mesh = self.get(name)
        self._require_authority(mesh, "invite minting")
        prefix = (prefix or "").strip()
        if not prefix:
            raise MeshError("give the ticket prefix shown by the invite list")
        matched = [t for t in mesh.invites if t.startswith(prefix)]
        if not matched:
            raise MeshError(f"no outstanding invite matches {prefix!r}")
        for t in matched:
            del mesh.invites[t]
        self._persist_def(mesh)
        return len(matched)

    async def invite_member(
        self,
        name: str,
        machine: str,
        session: str,
        *,
        handle: str = "",
        role: str = "",
        subroles: Sequence[str] = (),
    ) -> dict:
        """Owner-initiated enrolment: pull ``machine``'s ``session`` into the
        mesh (the CLI wizard / web "add remote member" path).

        Instead of carrying a ticket to the other machine by hand, the primary
        pushes an invitation to that daemon over the relay; the remote daemon
        validates the session and joins back through the ordinary
        join-by-address path, pre-approved by an embedded one-shot ticket.
        Trust model: backends on one relay belong to one operator (the relay's
        single backend token), so the remote daemon accepts without a local
        confirmation step.
        """
        mesh = self.get(name)
        self._require_authority(mesh, "membership")
        me = self._require_machine()
        if not machine or machine == me:
            raise MeshError(
                "pick another machine on the relay — local sessions join "
                f"with 'claunch mesh join {name}'"
            )
        if self.peer_transport is None:
            raise MeshError("relay uplink is not running — cannot reach peers")
        body = {
            "mesh": mesh.wire_name,
            "machine": me,
            "session": session,
            "handle": handle,
            "role": role,
            "subroles": list(subroles),
        }
        ticket = None
        if machine not in mesh.links:  # first contact needs the pre-approval
            ticket = self.invite(name)
            body["code"] = ticket["code"]
        try:
            resp = await self.peer_transport(machine, "/peer/mesh/invite", body)
        except (MeshError, PeerUnreachable):
            if ticket is not None:  # burn the unredeemed ticket
                try:
                    self.invite_revoke(name, self._ticket_token(ticket["code"])[:8])
                except MeshError:
                    pass  # already consumed or expired meanwhile
            raise
        member = resp.get("member") if isinstance(resp, dict) else None
        if not isinstance(member, dict):
            raise MeshError(f"unexpected invite response from {machine!r}")
        log.info(
            "mesh %r: invited %r (%s/%s) via push",
            mesh.name, member.get("handle"), machine, session,
        )
        return member

    @staticmethod
    def _ticket_token(code: str) -> str:
        try:
            return str(json.loads(
                base64.urlsafe_b64decode(code.encode("ascii")))["token"])
        except Exception:  # noqa: BLE001 — our own code should always parse
            return ""

    @staticmethod
    def _invite_age(created: str) -> float:
        try:
            dt = datetime.fromisoformat(created)
        except ValueError:
            return float("inf")  # unparseable = treat as expired
        return (datetime.now(timezone.utc) - dt).total_seconds()

    def _expire_invites(self, mesh: Mesh) -> None:
        stale = [
            t for t, created in mesh.invites.items()
            if self._invite_age(created) > self.invite_ttl
        ]
        for t in stale:
            del mesh.invites[t]
        if stale:
            self._persist_def(mesh)

    def _redeem_invite(self, mesh: Mesh, token: str) -> None:
        """Consume one ticket (constant-time match, TTL-checked)."""
        matched = next(
            (t for t in mesh.invites if secrets.compare_digest(
                t.encode("utf-8"), str(token).encode("utf-8"))),
            None,
        )
        if matched is None:
            raise MeshError("unknown or already-used invite code")
        created = mesh.invites.pop(matched)
        self._persist_def(mesh)
        if self._invite_age(created) > self.invite_ttl:
            raise MeshError("invite code has expired — mint a new one")

    def _check_link_token(self, mesh: Mesh, machine: str, token: str) -> None:
        """Authenticate an inbound peer call over the link to ``machine``.

        One check for both directions: since phase 7 an edge holds the same
        ``{token_in, token_out}`` pair whichever side ranks higher.
        """
        link = mesh.links.get(machine)
        expected = link.get("token_in") if link else None
        if expected is None or not secrets.compare_digest(
            str(token).encode("utf-8"), str(expected).encode("utf-8")
        ):
            raise MeshError("bad mesh peer token")

    def _check_primary_token(self, mesh: Mesh, machine: str, token: str) -> None:
        """As above, but the caller must also *be* our authority."""
        if not mesh.primary or machine != mesh.primary:
            raise MeshError("bad mesh peer token")
        self._check_link_token(mesh, machine, token)

    # -- primary-side: join requests, grants, guest lifecycle ------------ #
    def _admit_member(
        self, mesh: Mesh, machine: str, session: str, handle: str, role: str,
        parent: str = "", subroles: Sequence[str] = (),
    ):
        """Admit (or reclaim) a guest member — returns (member, created).

        The same (machine, session) re-joining reclaims its existing member
        record instead of conflicting: that is the mirror-lost recovery
        path, not a duplicate — and it keeps the wiring it was admitted with,
        because re-wiring it here would quietly undo every edge edited since.
        """
        for m in mesh.members.values():
            if m.machine == machine and m.session == session:
                return m, False
        handle = (handle or session).strip()
        if not _NAME_RE.match(handle):
            raise MeshError(
                f"invalid handle {handle!r}: use letters, digits, '.', '_' or '-'"
            )
        if handle in mesh.members:
            raise MeshConflict(
                f"handle {handle!r} is already taken in mesh {mesh.name!r}"
            )
        member = self._new_member(
            mesh, handle, session, role, subroles, machine=machine
        )
        mesh.members[handle] = member
        # An establishment join carries no parent: the mesh did not exist on
        # the guest until this call, so nothing over there was in it to have
        # spawned the joiner. The rules decide the rest.
        self._wire_member(mesh, member, str(parent or "").strip())
        return member, True

    def _ensure_ranked(self, mesh: Mesh, machine: str) -> None:
        """Append ``machine`` to the rank list if it is not there yet.

        Our own name goes in first, so the daemon that federates a mesh
        keeps the authority it already had as its sole owner.
        """
        if mesh.me and mesh.me not in mesh.peers:
            # After the insert, so `authority` is us — which it is: this is
            # the daemon federating its own mesh, and the blanks are its own.
            mesh.peers.insert(0, mesh.me)
            self._absolutize_roster(mesh)
        if machine and machine not in mesh.peers:
            mesh.peers.append(machine)

    @staticmethod
    def _absolutize_roster(mesh: Mesh) -> None:
        """Give every unstamped member the authority's machine.

        Before phase 7 a blank ``machine`` meant "the authority's own", which
        was unambiguous only because authority never moved. It moves now, so
        the roster has to be absolute: a member left blank would be claimed
        by whoever holds rank 0 next, and delivery would follow it to a
        daemon that does not have the session.

        The owner of a blank row is therefore ``mesh.authority``, not
        ``mesh.me`` — the same value only while we hold rank 0, which is why
        writing ``me`` was right everywhere except the one place it mattered.
        There is no ownership filter here and never was: on a mirror this
        rewrites the *authority's* rows, so stamping them with our own name
        hands us members whose sessions we do not host.

        Callers must have ``mesh.peers`` already in the state that makes
        ``mesh.authority`` the owner of the blanks — three call sites, four
        branches (``_migrate_v2`` has two), and each does, on its own side of
        its rank-list assignment. Moving a call across that assignment
        silently changes who the blanks are attributed to, and ``authority``
        falls back to ``mesh.me`` whenever ``peers`` is empty.
        """
        owner = mesh.authority
        if not owner:
            return
        for member in mesh.members.values():
            if not member.machine:
                member.machine = owner

    @staticmethod
    def _raise_seq_floor(mesh: Mesh) -> None:
        """Continue numbering above everything already written.

        Called whenever this daemon starts sequencing — at a handover, from
        either side — so ``(epoch, seq)`` stays strictly increasing even
        though the pen changed hands.
        """
        mesh.next_seq = max(
            [mesh.next_seq]
            + [int(m["seq"]) + 1 for m in mesh.messages if "seq" in m]
        )

    @staticmethod
    def _pair_key(a: str, b: str) -> str:
        return "|".join(sorted((a, b)))

    def _ensure_pair_links(self, mesh: Mesh) -> None:
        """Authority side: mint the missing peer-to-peer edge credentials.

        Phase 7's default topology is the complete graph, so every pair of
        non-authority peers gets a credential pair here and receives it on
        its next sync. Pairs whose machines have left are dropped.
        """
        if mesh.primary:
            return
        others = [m for m in mesh.peers if m != mesh.authority]
        wanted = {
            self._pair_key(a, b)
            for i, a in enumerate(others) for b in others[i + 1:]
        }
        for key in list(mesh.pair_links):
            if key not in wanted:
                del mesh.pair_links[key]
                mesh.edges.pop(key, None)
        for key in wanted:
            if key not in mesh.pair_links:
                a, b = key.split("|")
                mesh.pair_links[key] = {
                    f"token_{a}": secrets.token_urlsafe(18),  # a presents it
                    f"token_{b}": secrets.token_urlsafe(18),  # b presents it
                    "created_at": utcnow(),
                }
            mesh.edges.setdefault(key, True)

    def edge_table(self, mesh: Mesh) -> List[dict]:
        """Every edge of the graph with its state — what a diagram needs.

        Edges incident on the authority always read ``enabled``: they carry
        the sequenced log and are revoked rather than cut.

        ``cuttable`` is a property of the edge; ``editable`` is a property of
        *this* daemon's view of it — the authority may edit every edge, a
        peer only the ones it terminates. Shipping the answer keeps the rule
        in one place instead of re-deriving it in each client.
        """
        out = []
        for i, a in enumerate(mesh.peers):
            for b in mesh.peers[i + 1:]:
                key = self._pair_key(a, b)
                touches_authority = mesh.authority in (a, b)
                mine = mesh.me in (a, b)
                out.append(
                    {
                        "a": a,
                        "b": b,
                        "enabled": (
                            True if touches_authority
                            else bool(mesh.edges.get(key, True))
                        ),
                        "cuttable": not touches_authority,
                        "editable": not touches_authority
                        and (not mesh.primary or mine),
                    }
                )
        return out

    def _link_grants_for(self, mesh: Mesh, machine: str) -> List[dict]:
        """The edge credentials ``machine`` must hold, from its point of view.

        ``token_out`` is what it presents to the far end; ``token_in`` is
        what it should expect back. Both halves come from one authority, so
        the two peers cannot disagree about the edge.
        """
        grants = []
        for key, pair in sorted(mesh.pair_links.items()):
            a, b = key.split("|")
            if machine not in (a, b):
                continue
            other = b if machine == a else a
            grants.append(
                {
                    "machine": other,
                    "token_out": str(pair.get(f"token_{machine}") or ""),
                    "token_in": str(pair.get(f"token_{other}") or ""),
                    "created_at": str(pair.get("created_at") or ""),
                    "enabled": bool(mesh.edges.get(key, True)),
                }
            )
        return grants

    def _apply_link_grants(
        self, mesh: Mesh, grants: List[dict], sender: str = ""
    ) -> None:
        """Peer side: adopt the edges the authority brokered for us.

        ``sender`` is the daemon whose sync carried the grants; its own edge
        is never in the list (it brokered the others) and must survive the
        prune. Anchoring on the sender rather than on ``mesh.authority``
        matters at a handover, where the sync that promotes us comes from
        the *outgoing* authority and we are the new rank 0 ourselves.
        """
        if not isinstance(grants, list):
            return
        keep = {sender or mesh.authority}
        for g in grants:
            if not isinstance(g, dict):
                continue
            other = str(g.get("machine") or "")
            if not other or other == mesh.me:
                continue
            keep.add(other)
            mesh.links[other] = {
                "token_in": str(g.get("token_in") or ""),
                "token_out": str(g.get("token_out") or ""),
                "created_at": str(g.get("created_at") or "") or utcnow(),
                "enabled": bool(g.get("enabled", True)),
            }
        # An edge the authority no longer brokers is gone (peer revoked, or
        # the pair was dropped); keep only the authority link and live ones.
        for machine in [m for m in mesh.links if m not in keep]:
            del mesh.links[machine]
            mesh.link_cursors.pop(machine, None)

    def _register_guest(self, mesh: Mesh, machine: str, reply_token: str) -> None:
        """Mint (or re-mint) the credential pair for a peer machine and give
        it a rank (last — an existing peer keeps the rank it has)."""
        self._ensure_ranked(mesh, machine)
        previous = mesh.links.get(machine) or {}
        mesh.links[machine] = {
            "token_in": secrets.token_urlsafe(18),  # them -> us
            "token_out": str(reply_token),  # us -> them (their choice)
            "created_at": utcnow(),
            "enabled": bool(previous.get("enabled", True)),
        }
        mesh.link_cursors[machine] = len(mesh.messages)
        mesh.peer_status[machine] = {
            "ok": True, "error": None, "retry_at": 0.0, "backoff": 0.0,
            "last_sync": time.monotonic(), "roster_seen": mesh.roster_version,
        }
        self._ensure_pair_links(mesh)

    def _grant_payload(
        self, mesh: Mesh, machine: str, member: Optional[Member]
    ) -> dict:
        """Everything a peer needs to build its mirror + member (no member
        for a daemon attach).

        ``peers`` carries the whole rank list, so the newcomer knows which
        other daemons to open direct links with (phase 7's complete graph)
        and where it sits in the order.
        """
        mesh.link_cursors[machine] = len(mesh.messages)
        status = mesh.peer_status.get(machine)
        if status is not None:
            status["roster_seen"] = mesh.roster_version
            status["roles_seen"] = mesh.roles_version
        return {
            "token": mesh.links[machine]["token_in"],
            "members": [m.to_dict() for m in mesh.members.values()],
            # Shipped with the roster rather than left to the first sync: a
            # guest resolves recipients locally before forwarding them up, so
            # a mirror built without the edge table would refuse its own
            # member's first send — the wiring its join just performed is
            # invisible until a sync it has not had yet.
            "member_edges": dict(mesh.member_edges),
            "messages": list(mesh.messages),
            "policy": mesh.policy,
            "roles": {"doc": mesh.roles_doc, "version": mesh.roles_version},
            "member": member.to_dict() if member is not None else None,
            "cursor": len(mesh.messages),
            "peers": list(mesh.peers),
            "epoch": mesh.authority_epoch,
            "links": self._link_grants_for(mesh, machine),
            # Who created the mesh — the guest keys its mirror by it.
            "origin": mesh.origin or self._require_machine(),
        }

    def peer_join_request_accept(
        self,
        name: str,
        machine: str,
        session: str,
        handle: str,
        role: str,
        reply_token: str,
        code: str,
        subroles: Sequence[str] = (),
        offer: str = "",
    ) -> dict:
        """A remote daemon asks to enrol one of its sessions — or, with no
        ``session``, to attach itself with no member (daemon attach).

        Three outcomes: an already-trusted machine is auto-granted (mirror
        recovery); a valid invite ticket grants synchronously; anything else
        pends for operator approval. The self-declared ``machine`` cannot be
        verified here — but the grant travels via the relay to the *claimed*
        name, so only the daemon really registered under it can finish.
        """
        mesh = self._inbound(name, machine, authority=True)
        self._require_authority(mesh, "membership")
        if not _NAME_RE.match(machine or ""):
            raise MeshError("invalid peer machine name")
        if not str(reply_token or ""):
            raise MeshError("missing reply token")
        if machine == self._require_machine():
            raise MeshError("a daemon cannot join itself as a guest")
        offered = self._offer_matches(mesh, machine, offer)
        if not str(session or "").strip():
            return self._attach_request(mesh, machine, reply_token, code, offered)
        if machine in mesh.links or code or offered:
            if code:
                self._redeem_invite(mesh, code)
            if offered:
                mesh.offers.pop(machine, None)
            member, created = self._admit_member(
                mesh, machine, session, handle, role, subroles=subroles
            )
            self._register_guest(mesh, machine, reply_token)
            if created:
                mesh.roster_version += 1
            self._persist_def(mesh)
            self._persist_cursors(mesh)
            self._flush_guests_soon(mesh)
            log.info(
                "mesh %r: %r joined from %r (%s)",
                mesh.name, member.handle, machine,
                "ticket" if code else "trusted machine",
            )
            return {
                "granted": True,
                "grant": self._grant_payload(mesh, machine, member),
            }
        handle = (handle or session).strip()
        if not _NAME_RE.match(handle):
            raise MeshError(
                f"invalid handle {handle!r}: use letters, digits, '.', '_' or '-'"
            )
        if handle in mesh.members:
            raise MeshConflict(
                f"handle {handle!r} is already taken in mesh {name!r}"
            )
        rid = "req-" + uuid.uuid4().hex[:10]
        mesh.pending_requests[rid] = {
            "id": rid,
            "machine": machine,
            "session": session,
            "handle": handle,
            "role": role,
            "subroles": list(subroles),
            "reply_token": str(reply_token),
            "requested_at": utcnow(),
        }
        self._persist_def(mesh)
        log.info(
            "mesh %r: join request %s from %r (%r) awaits approval",
            mesh.name, rid, machine, handle,
        )
        return {"pending": True, "id": rid}

    @staticmethod
    def _offer_matches(mesh: Mesh, machine: str, token: str) -> bool:
        offer = mesh.offers.get(machine)
        if not offer or not token:
            return False
        return secrets.compare_digest(
            str(token).encode("utf-8"),
            str(offer.get("token") or "").encode("utf-8"),
        )

    def _attach_request(
        self, mesh: Mesh, machine: str, reply_token: str, code: str,
        offered: bool,
    ) -> dict:
        """The daemon-attach arm of :meth:`peer_join_request_accept`.

        Granted at once for an already-linked machine (a lost mirror asking
        again), a ticket, or the offer this daemon pushed to it; pended for
        the operator otherwise — a public listing advertises a mesh, it does
        not admit anyone.
        """
        if machine in mesh.links or code or offered:
            if code:
                self._redeem_invite(mesh, code)
            if machine not in mesh.links:
                mesh.roster_version += 1
            self._register_guest(mesh, machine, reply_token)
            mesh.offers.pop(machine, None)
            self._persist_def(mesh)
            self._persist_cursors(mesh)
            self._flush_guests_soon(mesh)
            log.info(
                "mesh %r: daemon %r attached (%s)", mesh.name, machine,
                "ticket" if code else "offer" if offered else "trusted machine",
            )
            return {
                "granted": True,
                "grant": self._grant_payload(mesh, machine, None),
            }
        for req in mesh.pending_requests.values():
            if req.get("machine") == machine and not req.get("session"):
                # the same daemon asking again: one entry, the newest token
                req["reply_token"] = str(reply_token)
                self._persist_def(mesh)
                return {"pending": True, "id": req["id"]}
        rid = "req-" + uuid.uuid4().hex[:10]
        mesh.pending_requests[rid] = {
            "id": rid,
            "machine": machine,
            "session": "",
            "handle": "",
            "role": "",
            "subroles": [],
            "reply_token": str(reply_token),
            "requested_at": utcnow(),
        }
        self._persist_def(mesh)
        log.info(
            "mesh %r: attach request %s from daemon %r awaits approval",
            mesh.name, rid, machine,
        )
        return {"pending": True, "id": rid}

    # -- owner side: who may discover a mesh ----------------------------- #
    def set_project(self, name: str, project: str) -> dict:
        """File a mesh under another project (blank = the default).

        Local bookkeeping only — the project is which of this daemon's
        listings shows the mesh, so it is allowed on a mirror as well and
        nothing is sent to the peers. An unknown project is refused.
        """
        mesh = self.get(name)
        try:
            project = projects.require(project).name
        except projects.ProjectError as exc:
            raise MeshError(str(exc)) from None
        mesh.project = "" if project == projects.DEFAULT else project
        self._persist_def(mesh)
        return {"mesh": mesh.name, "project": mesh.project or projects.DEFAULT}

    def _session_project(self, session: str) -> str:
        """The project ``session`` is filed under, "" when unknown."""
        try:
            sdef = self.manager.get(session).sdef
        except Exception:
            return ""
        name = projects.normalize(getattr(sdef, "project", None))
        return "" if name == projects.DEFAULT else name

    async def set_visibility(self, name: str, visibility: str) -> dict:
        """Publish a mesh to the relay (``public``), to named daemons only
        (``invited``), or to nobody (``private`` — withdraws every offer)."""
        mesh = self.get(name)
        self._require_authority(mesh, "publishing")
        visibility = str(visibility or "").strip().lower()
        if visibility not in VISIBILITIES:
            raise MeshError(
                f"visibility must be one of {', '.join(VISIBILITIES)}"
            )
        mesh.visibility = visibility
        withdrawn = []
        if visibility == "private":
            withdrawn = sorted(mesh.offers)
            mesh.offers.clear()
        self._persist_def(mesh)
        for machine in withdrawn:
            await self._push_offer_cancel(mesh.wire_name, machine)
        return {"mesh": mesh.name, "visibility": visibility,
                "withdrawn": withdrawn}

    async def offer_mesh(self, name: str, machine: str) -> dict:
        """Push an offer of this mesh to ``machine`` (the ``invited`` path).

        The offer carries a token only that daemon receives — the relay
        routes by registered name — and the token lets it attach without
        approval. A private mesh becomes ``invited`` by being offered.
        """
        mesh = self.get(name)
        self._require_authority(mesh, "publishing")
        me = self._require_machine()
        machine = str(machine or "").strip()
        if not _NAME_RE.match(machine) or machine == me:
            raise MeshError("pick another daemon on the relay to offer it to")
        if machine in mesh.links:
            raise MeshError(f"{machine!r} is already attached to mesh {name!r}")
        if self.peer_transport is None:
            raise MeshError("relay uplink is not running — cannot reach peers")
        previous = mesh.offers.get(machine)
        token = str((previous or {}).get("token") or secrets.token_urlsafe(18))
        await self.peer_transport(machine, "/peer/mesh/offer", {
            "mesh": mesh.wire_name,
            "machine": me,
            "token": token,
            "project": mesh.project,
            "members": len(mesh.members),
        })
        mesh.offers[machine] = {
            "token": token,
            "created_at": str((previous or {}).get("created_at") or utcnow()),
        }
        if mesh.visibility == "private":
            mesh.visibility = "invited"
        self._persist_def(mesh)
        log.info("mesh %r: offered to %r", mesh.name, machine)
        return {"mesh": mesh.name, "machine": machine,
                "visibility": mesh.visibility}

    async def cancel_offer(self, name: str, machine: str) -> dict:
        mesh = self.get(name)
        self._require_authority(mesh, "publishing")
        if mesh.offers.pop(machine, None) is None:
            raise MeshError(f"mesh {name!r} has no offer to {machine!r}")
        self._persist_def(mesh)
        notified = await self._push_offer_cancel(mesh.wire_name, machine)
        return {"mesh": mesh.name, "machine": machine, "notified": notified}

    async def _push_offer_cancel(self, name: str, machine: str) -> bool:
        """Best-effort: a daemon that misses it keeps a stale row whose
        token its owner now refuses."""
        if self.peer_transport is None:
            return False
        try:
            await self.peer_transport(machine, "/peer/mesh/offer", {
                "mesh": name, "machine": self._require_machine(),
                "cancel": True,
            })
        except Exception:  # noqa: BLE001 — best-effort notification
            return False
        return True

    def request_list(self, name: str) -> List[dict]:
        """Pending inbound join requests, oldest first (primary only)."""
        mesh = self.get(name)
        self._require_authority(mesh, "membership")
        return [
            {
                k: r.get(k)
                for k in ("id", "machine", "session", "handle", "role",
                          "requested_at")
            } | {"attach": not r.get("session")}
            for r in sorted(
                mesh.pending_requests.values(),
                key=lambda r: r.get("requested_at", ""),
            )
        ]

    async def approve_request(self, name: str, rid: str) -> dict:
        """Admit a pended join and deliver the grant (retried if needed)."""
        mesh = self.get(name)
        self._require_authority(mesh, "membership")
        req = mesh.pending_requests.pop(rid, None)
        if req is None:
            raise MeshError(f"no pending join request {rid!r} in mesh {name!r}")
        # The link this approval re-mints, kept so a guest that rejects the
        # grant is put back as it was (see _flush_grants).
        prior = dict(mesh.links.get(req["machine"]) or {})
        if not req.get("session"):  # a daemon attach: the link, no member
            if req["machine"] not in mesh.links:
                mesh.roster_version += 1
            self._register_guest(mesh, req["machine"], req["reply_token"])
            mesh.offers.pop(req["machine"], None)
            mesh.pending_grants[rid] = {
                "machine": req["machine"],
                "handle": "",
                "reply_token": req["reply_token"],
                **({"prior_link": prior} if prior else {}),
            }
            self._persist_def(mesh)
            self._persist_cursors(mesh)
            self._flush_guests_soon(mesh)
            await self._flush_grants(mesh)
            return {
                "id": rid,
                "handle": "",
                "machine": req["machine"],
                "attach": True,
                "delivered": rid not in mesh.pending_grants,
            }
        member, created = self._admit_member(
            mesh, req["machine"], req["session"], req["handle"], req["role"],
            subroles=_read_subroles(req.get("subroles")),
        )
        self._register_guest(mesh, req["machine"], req["reply_token"])
        if created:
            mesh.roster_version += 1
        mesh.pending_grants[rid] = {
            "machine": req["machine"],
            "handle": member.handle,
            "reply_token": req["reply_token"],
            **({"prior_link": prior} if prior else {}),
        }
        self._persist_def(mesh)
        self._persist_cursors(mesh)
        await self._flush_grants(mesh)
        return {
            "id": rid,
            "handle": member.handle,
            "machine": req["machine"],
            "delivered": rid not in mesh.pending_grants,
        }

    async def deny_request(self, name: str, rid: str) -> dict:
        """Drop a pended join and tell the requester (best-effort)."""
        mesh = self.get(name)
        self._require_authority(mesh, "membership")
        req = mesh.pending_requests.pop(rid, None)
        if req is None:
            raise MeshError(f"no pending join request {rid!r} in mesh {name!r}")
        self._persist_def(mesh)
        if self.peer_transport is not None:
            try:
                await self.peer_transport(
                    req["machine"],
                    "/peer/mesh/grant",
                    {
                        "mesh": mesh.wire_name,
                        "machine": self._require_machine(),
                        "request_id": rid,
                        "token": req["reply_token"],
                        "denied": True,
                    },
                )
            except Exception:  # noqa: BLE001 — the denial is best-effort
                pass
        return {"id": rid, "denied": True}

    async def _flush_grants(self, mesh: Mesh) -> None:
        """Deliver approved-but-undelivered grants (worker retries these)."""
        if mesh.primary or not mesh.pending_grants or self.peer_transport is None:
            return
        for rid, g in list(mesh.pending_grants.items()):
            machine = g["machine"]
            handle = str(g.get("handle") or "")  # "" = a daemon attach
            member = mesh.members.get(handle) if handle else None
            if (handle and member is None) or machine not in mesh.links:
                mesh.pending_grants.pop(rid, None)  # revoked meanwhile
                self._persist_def(mesh)
                continue
            status = mesh.peer_status.setdefault(
                machine,
                {"ok": None, "error": None, "retry_at": 0.0, "backoff": 0.0},
            )
            now = time.monotonic()
            if now < status.get("retry_at", 0.0):
                continue
            try:
                await self.peer_transport(
                    machine,
                    "/peer/mesh/grant",
                    {
                        "mesh": mesh.wire_name,
                        "machine": self.machine,
                        "request_id": rid,
                        "token": g["reply_token"],
                        "grant": self._grant_payload(mesh, machine, member),
                    },
                )
            except PeerUnreachable as exc:
                self._mark_peer_down(mesh, machine, exc)
                continue
            except MeshError as exc:
                # the guest REJECTED the grant (e.g. a local mesh of that
                # name appeared there): roll the admission back
                log.warning(
                    "mesh %r: guest %r rejected the grant for %r: %s",
                    mesh.name, machine, handle or "(attach)", exc,
                )
                mesh.pending_grants.pop(rid, None)
                prior = g.get("prior_link")
                if isinstance(prior, dict) and prior.get("token_in"):
                    # The guest was linked before this approval (an attach,
                    # or an earlier member): it keeps that link, and only
                    # what this grant added is undone.
                    mesh.links[machine] = prior
                    if handle:
                        self._rollback_admission(mesh, machine, handle,
                                                 keep_link=True)
                    else:
                        self._persist_def(mesh)
                elif handle:
                    self._rollback_admission(mesh, machine, handle)
                elif not any(m.machine == machine for m in mesh.members.values()):
                    self._remove_guest(mesh, machine)
                continue
            mesh.pending_grants.pop(rid, None)
            status.update({"ok": True, "error": None, "retry_at": 0.0,
                           "backoff": 0.0})
            self._persist_def(mesh)
            log.info(
                "mesh %r: grant delivered to %r (%r)",
                mesh.name, machine, g["handle"],
            )

    # -- renamed daemons: rewrite every reference to a relay name ----- #
    def _rename_in_mesh(self, mesh: Mesh, old: str, new: str) -> bool:
        """Rewrite machine ``old`` as ``new`` everywhere ``mesh`` names it.

        The rank list, the link credentials and their cursors and status,
        the brokered pair credentials and cut edges, the roster's machine
        stamps, pending requests/grants and offers. The message log is left
        alone: it records who sent what under the name they had then.
        Returns whether anything changed. Re-keying a mirror created on
        ``old`` is the caller's (it moves the mesh's directory).
        """
        changed = False
        if old in mesh.peers:
            if new in mesh.peers:
                raise MeshConflict(
                    f"mesh {mesh.name!r} already has a peer named {new!r}"
                )
            mesh.peers = [new if p == old else p for p in mesh.peers]
            changed = True
        for table in (mesh.links, mesh.link_cursors, mesh.peer_status,
                      mesh.pending_nudges, mesh.offers):
            if old in table:
                table[new] = table.pop(old)
                changed = True
        for key in list(mesh.pair_links):
            a, b = key.split("|")
            if old not in (a, b):
                continue
            pair = mesh.pair_links.pop(key)
            if f"token_{old}" in pair:
                pair[f"token_{new}"] = pair.pop(f"token_{old}")
            renamed = self._pair_key(new if a == old else a, new if b == old else b)
            mesh.pair_links[renamed] = pair
            changed = True
        for key in list(mesh.edges):
            a, b = key.split("|")
            if old in (a, b):
                mesh.edges[
                    self._pair_key(new if a == old else a, new if b == old else b)
                ] = mesh.edges.pop(key)
                changed = True
        for member in mesh.members.values():
            if member.machine == old:
                member.machine = new
                changed = True
        for rec in (list(mesh.pending_requests.values())
                    + list(mesh.pending_grants.values())):
            if rec.get("machine") == old:
                rec["machine"] = new
                changed = True
        if mesh.origin == old:
            changed = True
        return changed

    def _rekey(self, mesh: Mesh, old_key: str) -> None:
        """Move ``mesh`` (whose ``origin`` just changed) to its new key."""
        new_key = mesh.name
        if new_key == old_key:
            return
        self._meshes[new_key] = self._meshes.pop(old_key)
        task = self._workers.pop(old_key, None)
        if task is not None:
            self._workers[new_key] = task
        old_dir = self._mesh_dir(old_key)
        self._dirs.pop(old_key, None)
        new_dir = self._mesh_root() / new_key
        try:
            if old_dir.is_dir() and not new_dir.exists():
                old_dir.rename(new_dir)
        except OSError as exc:
            log.warning("mesh %r: keeping %s (%s)", new_key, old_dir, exc)
            self._dirs[new_key] = old_dir

    def _apply_rename(self, mesh: Mesh, old: str, new: str) -> Optional[str]:
        """Rename in one mesh, persist, fan out; returns the new key when
        the mesh itself was re-keyed (a mirror of the renamed daemon)."""
        if not self._rename_in_mesh(mesh, old, new):
            return None
        old_key = mesh.name
        rekeyed = None
        if mesh.origin == old:
            if f"{mesh.wire_name}@{new}" in self._meshes:
                raise MeshConflict(
                    f"mesh {mesh.wire_name}@{new} already exists here"
                )
            mesh.origin = new
            self._rekey(mesh, old_key)
            rekeyed = mesh.name
        if not mesh.primary:
            # The authority's rank list and roster changed: every other peer
            # learns the new name on its next sync.
            mesh.roster_version += 1
            self._ensure_pair_links(mesh)
            self._flush_guests_soon(mesh)
        self._persist_def(mesh)
        self._persist_cursors(mesh)
        return rekeyed

    def _rename_records(self, old: str, new: str) -> None:
        """Offers received and outgoing requests name daemons too."""
        moved = False
        for key in list(self._offers):
            rec = self._offers[key]
            if rec.get("machine") == old:
                rec["machine"] = new
                self._offers.pop(key)
                self._offers[f"{rec['mesh']}@{new}"] = rec
                moved = True
        if moved:
            self._persist_offers()
        touched = False
        for rec in self._outgoing.values():
            if rec.get("primary") == old:
                rec["primary"] = new
                touched = True
        if touched:
            self._persist_outgoing()

    def rename_peer(self, old: str, new: str) -> dict:
        """An operator's migration: daemon ``old`` is now called ``new``.

        Rewrites every mesh here that names it (and re-keys mirrors of the
        meshes it created, ``dev@old`` -> ``dev@new``). A renamed daemon
        tells its peers itself (``/peer/mesh/renamed``) once it is back on
        the relay; this is for when it could not — it was offline, or this
        daemon was.
        """
        old = str(old or "").strip()
        new = str(new or "").strip()
        for n in (old, new):
            if not _NAME_RE.match(n):
                raise MeshError(f"invalid daemon name {n!r}")
        if old == new:
            raise MeshError("old and new names are the same")
        if new == LOCAL_HOST:
            raise MeshError(f"{LOCAL_HOST!r} is reserved for this daemon")
        if self._is_me(old) or self._is_me(new):
            raise MeshError(
                "that is this daemon's own name — change the relay name "
                "instead; peers are told on the next connection"
            )
        changed, rekeyed = [], []
        for mesh in list(self._meshes.values()):
            before = mesh.name
            if not self._rename_in_mesh_dry(mesh, old, new):
                continue
            key = self._apply_rename(mesh, old, new)
            changed.append(before)
            if key:
                rekeyed.append({"from": before, "to": key})
        self._rename_records(old, new)
        log.info("renamed peer %r -> %r in %d mesh(es)", old, new, len(changed))
        return {"old": old, "new": new, "meshes": changed, "rekeyed": rekeyed}

    @staticmethod
    def _rename_in_mesh_dry(mesh: Mesh, old: str, new: str) -> bool:
        """Does ``mesh`` name ``old`` at all? (Checked before any write, so
        a conflict on one mesh does not leave another half-renamed.)"""
        if new in mesh.peers and old in mesh.peers:
            raise MeshConflict(
                f"mesh {mesh.name!r} has both {old!r} and {new!r} as peers"
            )
        return (
            old in mesh.peers or old in mesh.links or old in mesh.offers
            or mesh.origin == old
            or any(m.machine == old for m in mesh.members.values())
            or any(r.get("machine") == old for r in mesh.pending_requests.values())
        )

    def _renamed_self(self, mesh: Mesh, old: str, new: str) -> None:
        """Our own relay name changed from ``old`` (the name this mesh was
        last written with): restamp our side and queue a notice to every
        linked peer, which is how they learn it — the relay routes to the
        new name, and the link token proves it is still us."""
        self._rename_in_mesh(mesh, old, new)
        pending = [p for p in mesh.links if p != new]
        if pending:
            previous = (mesh.rename_notice or {}).get("old")
            mesh.rename_notice = {
                # a second rename before the first reached everyone: peers
                # still know us by the FIRST name
                "old": previous or old,
                "pending": pending,
            }
        if not mesh.primary:
            mesh.roster_version += 1
            self._ensure_pair_links(mesh)
        log.info("mesh %r: this daemon was renamed %r -> %r", mesh.name, old, new)
        mesh.me = new
        self._persist_def(mesh)

    async def _flush_rename_notice(self, mesh: Mesh) -> None:
        notice = mesh.rename_notice or {}
        old = str(notice.get("old") or "")
        for machine in list(notice.get("pending") or []):
            if machine not in mesh.links:
                notice["pending"].remove(machine)
                continue
            try:
                await self._peer_call(
                    mesh, machine, "/peer/mesh/renamed", {"old": old}
                )
            except PeerUnreachable:
                continue  # the worker's next pass retries
            except MeshError as exc:
                log.warning(
                    "mesh %r: %r refused our rename notice: %s",
                    mesh.name, machine, exc,
                )
            notice["pending"].remove(machine)
        if not notice.get("pending"):
            mesh.rename_notice = None
        self._persist_def(mesh)

    def peer_renamed_accept(
        self, name: str, machine: str, token: str, old: str
    ) -> dict:
        """A linked peer tells us it is now ``machine``, formerly ``old``.

        Authenticated on the link we hold for ``old``: only the daemon that
        holds that link's credentials can move it to a new name.
        """
        if not _NAME_RE.match(old or "") or not _NAME_RE.match(machine or ""):
            raise MeshError("invalid rename notice")
        if old == machine:
            return {"ok": True}
        mesh = self._inbound(name, old, token)
        self._check_link_token(mesh, old, token)
        rekeyed = self._apply_rename(mesh, old, machine)
        self._rename_records(old, machine)
        log.info(
            "mesh %r: peer %r is now %r", mesh.name, old, machine,
        )
        return {"ok": True, "mesh": rekeyed or mesh.name}

    def _unrank(self, mesh: Mesh, machine: str) -> None:
        """Drop every trace of a departed peer: rank, edges, brokered pairs."""
        mesh.links.pop(machine, None)
        mesh.link_cursors.pop(machine, None)
        mesh.peer_status.pop(machine, None)
        mesh.pending_nudges.pop(machine, None)
        if machine in mesh.peers:
            mesh.peers.remove(machine)
        # A single remaining peer (ourselves) is not a graph any more.
        if mesh.peers == [mesh.me]:
            mesh.peers = []
        self._ensure_pair_links(mesh)

    def _rollback_admission(
        self, mesh: Mesh, machine: str, handle: str, *, keep_link: bool = False,
    ) -> None:
        mesh.members.pop(handle, None)
        mesh.remote_activity.pop(handle, None)
        mesh.remote_lineage.pop(handle, None)
        # The wiring the admission made goes with it.
        for key in [k for k in mesh.member_edges if handle in k.split("|")]:
            mesh.member_edges.pop(key, None)
        if not keep_link and not any(
            m.machine == machine for m in mesh.members.values()
        ):
            self._unrank(mesh, machine)
        mesh.roster_version += 1
        self._persist_def(mesh)
        self._persist_cursors(mesh)
        self._flush_guests_soon(mesh)

    def _remove_guest(self, mesh: Mesh, machine: str) -> List[str]:
        """Authority side: drop a guest's rank, credentials and members.

        Shared by an operator's revoke and a guest's own detach; the caller
        decides whether the guest is told."""
        self._unrank(mesh, machine)
        for rid in [
            r for r, g in mesh.pending_grants.items() if g["machine"] == machine
        ]:
            mesh.pending_grants.pop(rid, None)
        removed = [h for h, m in mesh.members.items() if m.machine == machine]
        for h in removed:
            mesh.members.pop(h, None)
            mesh.remote_activity.pop(h, None)
            mesh.remote_lineage.pop(h, None)
            self._drop_leases(mesh, h)
        mesh.roster_version += 1
        self._persist_def(mesh)
        self._persist_cursors(mesh)
        self._flush_guests_soon(mesh)
        return removed

    def peer_detach_accept(self, name: str, machine: str, token: str) -> dict:
        """A guest daemon detaches itself (and every member it hosts)."""
        mesh = self._inbound(name, machine, token)
        self._require_authority(mesh, "guest management")
        self._check_link_token(mesh, machine, token)
        removed = self._remove_guest(mesh, machine)
        log.info(
            "mesh %r: guest %r detached (%d member(s) removed)",
            mesh.name, machine, len(removed),
        )
        return {"ok": True, "removed_members": removed}

    async def revoke_guest(self, name: str, machine: str) -> dict:
        """Unlink a guest machine: drop its members, credentials and mirror."""
        mesh = self.get(name)
        self._require_authority(mesh, "guest management")
        guest = mesh.links.get(machine)
        if guest is None:
            raise MeshError(f"no guest {machine!r} linked to mesh {name!r}")
        removed = self._remove_guest(mesh, machine)
        if self.peer_transport is not None:
            try:
                await self.peer_transport(
                    machine,
                    "/peer/mesh/unlink",
                    {
                        "mesh": mesh.wire_name,
                        "machine": self._require_machine(),
                        "token": str(guest.get("token_out") or ""),
                    },
                )
            except Exception:  # noqa: BLE001 — best-effort notification
                pass
        log.info(
            "mesh %r: guest %r revoked (%d member(s) removed)",
            mesh.name, machine, len(removed),
        )
        return {"machine": machine, "removed_members": removed}

    # -- guest-side: grant + unlink handlers, outgoing bookkeeping -------- #
    def peer_grant_accept(
        self,
        name: str,
        machine: str,
        request_id: str,
        token: str,
        denied: bool,
        grant,
    ) -> dict:
        """The primary answers one of our pending join requests."""
        rec = self._outgoing.get(str(request_id))
        if (
            rec is None
            or rec.get("mesh") != name
            or rec.get("primary") != machine
        ):
            raise MeshError("unknown join request")
        if not secrets.compare_digest(
            str(token).encode("utf-8"),
            str(rec.get("reply_token") or "").encode("utf-8"),
        ):
            raise MeshError("bad grant token")
        del self._outgoing[str(request_id)]
        self._persist_outgoing()
        if denied:
            log.info(
                "mesh %r: join request %s was denied by %r",
                name, request_id, machine,
            )
            return {"ok": True, "denied": True}
        member = self._adopt_grant(
            name, machine, str(rec.get("reply_token") or ""),
            grant if isinstance(grant, dict) else {},
            attach=bool(rec.get("attach")),
            project=str(rec.get("project") or ""),
        )
        if member is None:
            return {"ok": True, "attached": True}
        return {"ok": True, "handle": member.handle}

    async def peer_invite_accept(
        self,
        name: str,
        machine: str,
        session: str,
        handle: str,
        role: str,
        code: str,
        subroles: Sequence[str] = (),
    ) -> dict:
        """A mesh owner on ``machine`` pushes an invitation for our ``session``.

        We simply run the ordinary join-by-address back at the claimed owner:
        the embedded ticket (or an existing trusted link) makes it synchronous,
        and every join-side validation — session exists and is alive, handle
        shape, name collision, code/address cross-check — applies unchanged.
        Trust model: same relay = same operator (one backend token), so no
        local confirmation gate.
        """
        if not machine:
            raise MeshError("invitation carries no origin machine")
        result = await self.join(
            f"{name}@{machine}", session, handle=handle, role=role,
            subroles=subroles,
            code=code or None,
        )
        if isinstance(result, dict):  # pended — the inviter failed to pre-approve
            self.cancel_request(str(result.get("request_id") or ""))
            raise MeshError(
                "invitation was not pre-approved by the inviting daemon"
            )
        return {"member": result.to_dict()}

    def peer_unlink_accept(self, name: str, machine: str, token: str) -> dict:
        """The primary revoked us (or deleted the mesh): drop the mirror."""
        mesh = self._inbound(name, machine, token)
        if not mesh.primary:
            raise MeshError("not a mirror")
        self._check_primary_token(mesh, machine, token)
        self._drop_mesh(name)
        log.info("mesh %r: unlinked by primary %r — mirror dropped", name, machine)
        return {"ok": True}

    def outgoing_list(self) -> List[dict]:
        """Our join requests still awaiting a primary's decision."""
        return [
            {
                k: r.get(k)
                for k in ("request_id", "mesh", "primary", "session", "handle",
                          "role", "requested_at", "attach")
            }
            for r in sorted(
                self._outgoing.values(),
                key=lambda r: r.get("requested_at", ""),
            )
        ]

    def cancel_request(self, request_id: str) -> dict:
        """Forget an outgoing join request locally (the primary's operator
        still sees — and should deny — the stale server-side entry)."""
        rec = self._outgoing.pop(str(request_id), None)
        if rec is None:
            raise MeshError(f"no outgoing join request {request_id!r}")
        self._persist_outgoing()
        return {"request_id": str(request_id), "cancelled": True}

    def _persist_outgoing(self) -> None:
        root = self._mesh_root()
        try:
            root.mkdir(parents=True, exist_ok=True)
            path = root / "outgoing_joins.json"
            with atomic.scratch(path) as tmp:
                tmp.write_text(
                    json.dumps(list(self._outgoing.values()), indent=2),
                    encoding="utf-8",
                )
                atomic.replace(tmp, path)
        except OSError as exc:
            log.warning("cannot persist outgoing join requests: %s", exc)

    def peer_join_accept(
        self, name: str, machine: str, token: str,
        session: str, handle: str, role: str, parent: str = "",
        subroles: Sequence[str] = (),
    ) -> dict:
        """A guest daemon asks to enrol one of its sessions as a member."""
        mesh = self._inbound(name, machine, token)
        self._require_authority(mesh, "membership")
        self._check_link_token(mesh, machine, token)
        handle = (handle or session).strip()
        if not _NAME_RE.match(handle):
            raise MeshError(
                f"invalid handle {handle!r}: use letters, digits, '.', '_' or '-'"
            )
        if handle in mesh.members:
            raise MeshConflict(
                f"handle {handle!r} is already taken in mesh {name!r}"
            )
        for m in mesh.members.values():
            if m.machine == machine and m.session == session:
                raise MeshConflict(
                    f"session {session!r} on {machine!r} is already in mesh "
                    f"{name!r} as {m.handle!r}"
                )
        member = self._new_member(
            mesh, handle, session, role, subroles, machine=machine
        )
        mesh.members[handle] = member
        # The guest names the parent (only it can see its own session tree);
        # a name that is not a member of this mesh wires as a root.
        self._wire_member(mesh, member, str(parent or "").strip())
        self._roster_changed(mesh)
        log.info("mesh %r: %r joined from guest %r", mesh.name, handle, machine)
        return {
            **member.to_dict(),
            "cursor": len(mesh.messages),
            "member_edges": dict(mesh.member_edges),
        }

    def peer_leave_accept(
        self, name: str, machine: str, token: str, handle: str
    ) -> dict:
        """A guest daemon withdraws one of its OWN members."""
        mesh = self._inbound(name, machine, token)
        self._require_authority(mesh, "membership")
        self._check_link_token(mesh, machine, token)
        member = mesh.members.get(handle)
        if member is None:
            raise MeshError(f"no member {handle!r} in mesh {name!r}")
        if member.machine != machine:
            raise MeshError(
                f"{handle!r} does not belong to daemon {machine!r}"
            )
        mesh.members.pop(handle, None)
        mesh.remote_activity.pop(handle, None)
        mesh.remote_lineage.pop(handle, None)
        self._drop_leases(mesh, handle)
        self._roster_changed(mesh)
        return member.to_dict()

    def peer_send_accept(
        self, name: str, machine: str, token: str, message: dict
    ) -> dict:
        """A guest daemon forwards a member's send for sequencing."""
        mesh = self._inbound(name, machine, token)
        self._require_authority(mesh, "sequencing")
        self._check_link_token(mesh, machine, token)
        if not isinstance(message, dict):
            raise MeshError("bad message payload")
        mid = str(message.get("id") or "")
        if not mid:
            raise MeshError("message needs an id")
        if mid in mesh.seen_ids:
            # Idempotent retry (the guest crashed or timed out mid-call):
            # acknowledge without re-sequencing.
            return {"id": mid, "duplicate": True, "queued": False,
                    "recipients": []}
        external = bool(message.get("external"))
        sender = str(message.get("from") or "")
        to = message.get("to")
        if not isinstance(to, (str, list)) or not to:
            raise MeshError("'to' must be '*', a handle, or a list of handles")
        sections = message.get("sections")
        ref = message.get(REF_KEY)
        result = self._send_core(
            mesh,
            sender,
            to,
            str(message.get("body") or ""),
            external=external,
            type=str(message.get("type") or "say"),
            reply_to=str(message.get("reply_to") or "") or None,
            sections=sections if isinstance(sections, dict) else None,
            ref=ref if isinstance(ref, dict) else None,
            msg_id=mid,
            sender_machine=None if external else machine,
            ts=str(message.get("ts") or "") or None,
        )
        self._flush_guests_soon(mesh)
        return result

    # -- peer operations: read a member's checkout, coordinate on keys --- #
    def _ops_actor(self, mesh: Mesh, actor: str) -> Member:
        """The member the calling session is — the identity every op needs."""
        member = self.member_for_session(mesh, actor)
        if member is None:
            raise MeshError(
                f"session {actor!r} is not a member of mesh {mesh.name!r}"
            )
        return member

    def resolve_peer(self, mesh: Mesh, ref: str) -> Member:
        """The member a peer operation names.

        Three forms, tried in this order: a handle, a member's session name,
        or a daemon-qualified ``<machine>/<session>``. The handle comes first
        so that no address which already worked changes meaning. A session
        name is accepted because that is usually how the caller was told about
        the peer — the spawn, the board and the terminal all name sessions
        rather than handles — and the daemon to route to then comes from the
        member row instead of having to be typed alongside it. The qualified
        form settles the one case a bare session name cannot: two daemons each
        running a session of that name.
        """
        ref = str(ref or "").strip()
        if not ref:
            raise MeshError("no member named")
        if ref in mesh.members:
            return mesh.members[ref]
        machine, sep, session = ref.rpartition("/")
        if not sep:
            machine, session = "", ref
        hits = [
            m for m in mesh.members.values()
            if m.session == session
            and (not machine or self.machine_name(mesh, m) == machine)
        ]
        if len(hits) == 1:
            return hits[0]
        if not hits:
            raise MeshError(
                f"no member {ref!r} in mesh {mesh.name!r} — name a handle, a "
                f"member's session, or <machine>/<session>"
            )
        where = ", ".join(
            f"{self.machine_name(mesh, m) or 'here'}/{m.session} ({m.handle})"
            for m in sorted(hits, key=lambda m: m.handle)
        )
        raise MeshError(
            f"session {session!r} runs on more than one daemon in mesh "
            f"{mesh.name!r} — qualify it as <machine>/<session>: {where}"
        )

    def _ops_target(self, mesh: Mesh, actor: Member, handle: str) -> Member:
        """The member whose checkout is being read, after the graph check.

        The member graph is the ACL for messages, and it is the ACL here for
        the same reason: a lead that keeps two workers apart so their work
        stays independent did not mean for one to read the other's tree.
        """
        target = self.resolve_peer(mesh, handle)
        if not mesh.connected(actor.handle, target.handle):
            raise MeshError(
                f"{actor.handle!r} is not connected to {target.handle!r} — "
                f"the member graph decides who may read whose checkout"
            )
        return target

    def _session_cwd(self, session: str) -> str:
        try:
            sess = self.manager.get(session)
        except ManagerError:
            raise MeshError(f"session {session!r} is not running here") from None
        return str(getattr(sess.sdef, "cwd", "") or "")

    async def ops_file(
        self, name: str, actor: str, handle: str, path: str,
        *, max_bytes: Optional[int] = None,
    ) -> dict:
        """Read ``path`` from inside member ``handle``'s working directory."""
        mesh = self.get(name)
        me = self._ops_actor(mesh, actor)
        target = self._ops_target(mesh, me, handle)
        if self._is_local(mesh, target):
            cwd = self._session_cwd(target.session)
            try:
                result = await asyncio.to_thread(
                    mesh_ops.read_file, cwd, path, max_bytes=max_bytes
                )
            except mesh_ops.OpsError as exc:
                raise MeshError(str(exc)) from None
            return {
                "member": target.handle, "session": target.session,
                "machine": self.machine, **result,
            }
        payload = await self._peer_call(
            mesh, target.machine, "/peer/ops/file",
            {"session": target.session, "actor": me.handle,
             "path": path, "max_bytes": max_bytes},
        )
        return {
            "member": target.handle, "session": target.session,
            "machine": target.machine, **payload,
        }

    async def ops_git(
        self, name: str, actor: str, handle: str, op: str,
        args: Optional[dict] = None,
    ) -> dict:
        """Run one whitelisted read-only git query in ``handle``'s checkout."""
        mesh = self.get(name)
        me = self._ops_actor(mesh, actor)
        target = self._ops_target(mesh, me, handle)
        if self._is_local(mesh, target):
            cwd = self._session_cwd(target.session)
            try:
                result = await asyncio.to_thread(mesh_ops.git_query, cwd, op, args)
            except mesh_ops.OpsError as exc:
                raise MeshError(str(exc)) from None
            return {
                "member": target.handle, "session": target.session,
                "machine": self.machine, **result,
            }
        payload = await self._peer_call(
            mesh, target.machine, "/peer/ops/git",
            {"session": target.session, "actor": me.handle,
             "op": op, "args": args or {}},
        )
        return {
            "member": target.handle, "session": target.session,
            "machine": target.machine, **payload,
        }

    def _lease_apply(
        self, mesh: Mesh, op: str, key: str, holder: str, ttl, note: str,
    ) -> dict:
        """Authority side: one lease operation, persisted."""
        reg = mesh.leases
        try:
            if op == "acquire":
                lease = reg.acquire(key, holder, ttl=ttl, note=note)
                result = {"ok": True, "lease": lease}
            elif op == "renew":
                lease = reg.renew(key, holder, ttl=ttl)
                result = {"ok": True, "lease": lease}
            elif op == "release":
                result = {"ok": True, **reg.release(key, holder)}
            elif op == "list":
                return {"ok": True, "leases": reg.list(holder=key)}
            else:
                raise MeshError(
                    f"unknown lease op {op!r} (acquire, renew, release, list)"
                )
        except mesh_ops.LeaseHeld as exc:
            # Not an error to the caller: the answer IS the holder.
            return {"ok": False, "held_by": exc.lease["holder"],
                    "lease": exc.lease, "error": str(exc)}
        except mesh_ops.OpsError as exc:
            raise MeshError(str(exc)) from None
        reg.prune()
        self._persist_leases(mesh)
        return result

    async def lease(
        self, name: str, actor: str, op: str, key: str = "",
        *, ttl=None, note: str = "",
    ) -> dict:
        """acquire / renew / release / list a coordination lease as ``actor``.

        The holder is always the calling member's handle — a session cannot
        take or drop a key in somebody else's name. ``list`` takes ``key``
        as an optional holder filter. On a mirror the call is forwarded to
        the authority, which is the only daemon that may answer it.
        """
        mesh = self.get(name)
        me = self._ops_actor(mesh, actor)
        if mesh.primary:
            payload = await self._peer_call_primary(
                mesh, "/peer/ops/lease",
                {"op": op, "key": key, "holder": me.handle,
                 "ttl": ttl, "note": note},
            )
            return {"holder": me.handle, "authority": mesh.authority, **payload}
        return {
            "holder": me.handle, "authority": mesh.authority,
            **self._lease_apply(mesh, op, key, me.handle, ttl, note),
        }

    def _peer_ops_session(self, mesh: Mesh, session: str) -> str:
        """cwd of ``session`` if it wears a member of ``mesh`` on this daemon.

        A peer may only read the checkouts of sessions that are in the mesh
        the link belongs to — the link token authenticates the DAEMON, and
        this is what scopes it to the mesh's own members.
        """
        member = self.member_for_session(mesh, session)
        if member is None:
            raise MeshError(
                f"session {session!r} is not a member of mesh {mesh.name!r} here"
            )
        return self._session_cwd(session)

    def peer_ops_file_accept(
        self, name: str, machine: str, token: str, session: str, path: str,
        max_bytes=None,
    ) -> dict:
        mesh = self._inbound(name, machine, token)
        self._check_link_token(mesh, machine, token)
        cwd = self._peer_ops_session(mesh, session)
        try:
            return mesh_ops.read_file(cwd, path, max_bytes=max_bytes)
        except mesh_ops.OpsError as exc:
            raise MeshError(str(exc)) from None

    def peer_ops_git_accept(
        self, name: str, machine: str, token: str, session: str, op: str,
        args: Optional[dict] = None,
    ) -> dict:
        mesh = self._inbound(name, machine, token)
        self._check_link_token(mesh, machine, token)
        cwd = self._peer_ops_session(mesh, session)
        try:
            return mesh_ops.git_query(cwd, op, args)
        except mesh_ops.OpsError as exc:
            raise MeshError(str(exc)) from None

    def peer_lease_accept(
        self, name: str, machine: str, token: str, op: str, key: str,
        holder: str, ttl=None, note: str = "",
    ) -> dict:
        """A peer forwards one of its members' lease operations to us."""
        mesh = self._inbound(name, machine, token)
        self._require_authority(mesh, "leasing")
        self._check_link_token(mesh, machine, token)
        member = mesh.members.get(holder)
        if member is None or member.machine != machine:
            raise MeshError(
                f"{holder!r} is not a member from daemon {machine!r}"
            )
        return self._lease_apply(mesh, op, key, holder, ttl, note)

    # -- remote-shadow sessions (daemon/shadow.py) ---------------------- #
    # Another daemon's operator may LOOK at this daemon's mesh members: a
    # card, a terminal that only outputs, and the session line. The link
    # token authenticates the viewing daemon; membership in the mesh that
    # link belongs to scopes it -- exactly the rule peer ops read by.
    def peer_shadow_members(self, name: str, machine: str, token: str) -> List[Member]:
        """This daemon's own members of mesh ``name``, for linked ``machine``."""
        mesh = self._inbound(name, machine, token)
        self._check_link_token(mesh, machine, token)
        return [
            mesh.members[h] for h in sorted(mesh.members)
            if self._is_local(mesh, mesh.members[h])
        ]

    def peer_shadow_member(
        self, name: str, machine: str, token: str, session: str,
    ) -> Member:
        """The member ``session`` wears in mesh ``name`` here, or refused."""
        mesh = self._inbound(name, machine, token)
        self._check_link_token(mesh, machine, token)
        member = self.member_for_session(mesh, session)
        if member is None:
            raise MeshError(
                f"session {session!r} is not a member of mesh {mesh.name!r} here"
            )
        return member

    def host_machine(self, mesh: Mesh, member: Member) -> str:
        """The daemon ``member``'s session runs on — "" when it is this one.

        Unlike :meth:`machine_name` this never answers blank for a remote
        member: an unstamped row on a mirror is the authority's own.
        """
        if self._is_local(mesh, member):
            return ""
        return member.machine or mesh.authority

    def shadow_targets(self) -> List[dict]:
        """Every other daemon's member of every mesh held here, grouped per
        (machine, session) — one session in two meshes is one shadow.

        Each row: ``{machine, session, meshes: [{mesh, wire, handle, role,
        roles, linked}]}``. ``linked`` says whether this daemon holds a link
        to that machine in that mesh — without one there is nobody to ask.
        """
        rows: Dict[Tuple[str, str], dict] = {}
        for mesh in self.list():
            for handle in sorted(mesh.members):
                member = mesh.members[handle]
                host = self.host_machine(mesh, member)
                if not host or not member.session:
                    continue
                row = rows.setdefault(
                    (host, member.session),
                    {"machine": host, "session": member.session, "meshes": []},
                )
                row["meshes"].append({
                    "mesh": mesh.name,
                    "wire": mesh.wire_name,
                    "handle": member.handle,
                    "role": member.role,
                    "roles": list(member.roles),
                    "linked": host in mesh.links,
                })
        return [rows[k] for k in sorted(rows)]

    def shadow_route(self, machine: str, session: str) -> Tuple[Mesh, Member]:
        """The mesh (and member row) to reach ``machine``'s ``session`` through.

        Refused unless that session is a member of a mesh held here and this
        daemon holds a link to its machine in that mesh: a shadow is only
        ever of a mesh member, never of an arbitrary session on a peer.
        """
        unlinked = False
        for mesh in self.list():
            for member in mesh.members.values():
                if member.session != session:
                    continue
                if self.host_machine(mesh, member) != machine:
                    continue
                if machine in mesh.links:
                    return mesh, member
                unlinked = True
        if unlinked:
            raise MeshError(
                f"no link to daemon {machine!r} in any mesh {session!r} is in"
            )
        raise MeshError(
            f"{machine}/{session} is not a member of any mesh on this daemon"
        )

    async def shadow_call(self, machine: str, session: str, path: str,
                          body: dict) -> dict:
        """One ``/peer/shadow/*`` request about ``machine``'s ``session``."""
        mesh, _member = self.shadow_route(machine, session)
        return await self._peer_call(mesh, machine, path, {"session": session, **body})

    async def shadow_cards(self, mesh_name: str, machine: str) -> dict:
        """``/peer/shadow/cards`` for mesh ``mesh_name``'s members on ``machine``."""
        mesh = self.get(mesh_name)
        if machine not in mesh.links:
            raise MeshError(f"no link to daemon {machine!r} in mesh {mesh.name!r}")
        return await self._peer_call(mesh, machine, "/peer/shadow/cards", {})

    async def shadow_stream(self, machine: str, session: str):
        """Open ``/peer/shadow/stream`` for ``machine``'s ``session``: the
        live bridge, unparsed (``shadow.open_stream`` reads it)."""
        mesh, _member = self.shadow_route(machine, session)
        if self.peer_streamer is None:
            raise PeerUnreachable(
                f"relay uplink is not running — cannot reach {machine!r}"
            )
        link = mesh.links.get(machine) or {}
        return await self.peer_streamer(
            machine,
            "/peer/shadow/stream",
            {
                "mesh": mesh.wire_name,
                "machine": self._require_machine(),
                "token": str(link.get("token_out") or ""),
                "session": session,
            },
        )

    # -- peer-side handlers --------------------------------------------- #
    def _ingest_message(self, m: dict, origin: str) -> Optional[dict]:
        """Normalise a message that arrived over the wire, or None if unusable.

        Shared by the sequenced sync and the fast path so both produce
        byte-identical log entries for the same message.
        """
        if not isinstance(m, dict) or not isinstance(m.get("body"), str):
            return None
        mid = str(m.get("id") or "")
        if not mid:
            return None
        msg = {
            "id": mid,
            "ts": str(m.get("ts") or utcnow()),
            "from": str(m.get("from") or origin),
            "to": m.get("to") if isinstance(m.get("to"), (str, list)) else "*",
            "type": str(m.get("type") or "say").strip().lower() or "say",
            "body": _CTRL_RE.sub("", m["body"]),
        }
        for key in ("seq", "epoch"):
            if m.get(key) is not None:
                try:
                    msg[key] = int(m[key])
                except (TypeError, ValueError):
                    pass
        if m.get("reply_to"):
            msg["reply_to"] = str(m["reply_to"])
        if isinstance(m.get(REF_KEY), dict) and m[REF_KEY]:
            # Relayed as it arrived. A pointer this daemon cannot follow (it
            # names a run on another machine) is still worth keeping: dropping
            # it would leave the two histories describing different messages.
            msg[REF_KEY] = m[REF_KEY]
        # Batch fields ride along so this daemon can slice deliveries for
        # its own local members.
        if isinstance(m.get("sections"), dict):
            sections = {
                str(h): {
                    "text": _CTRL_RE.sub("", str(sec.get("text") or "")),
                    **(
                        {"type": str(sec["type"]).strip().lower()}
                        if sec.get("type") else {}
                    ),
                }
                for h, sec in m["sections"].items()
                if isinstance(sec, dict) and sec.get("text")
            }
            if sections:
                msg["sections"] = sections
                msg["shared"] = _CTRL_RE.sub("", str(m.get("shared") or ""))
        return msg

    def _fold_provisional(self, mesh: Mesh, mid: str) -> None:
        """Retire the fast-path copy of ``mid`` now that the log has it.

        Members that already had it injected keep the id in ``delivered_ids``
        so the sequenced copy does not reach their terminal a second time.
        """
        index = next(
            (i for i, m in enumerate(mesh.provisional) if m.get("id") == mid),
            None,
        )
        if index is None:
            return
        mesh.provisional.pop(index)

    def peer_deliver_accept(
        self, name: str, machine: str, token: str, message: dict
    ) -> dict:
        """Fast path: a peer hands us a send its authority has not sequenced.

        Only reachability is claimed here, never order — the message goes
        into ``provisional`` and reaches local terminals right away, and the
        authoritative copy folds over it whenever the authority comes back.
        """
        mesh = self._inbound(name, machine, token)
        self._check_link_token(mesh, machine, token)
        if not mesh.linked(machine):
            raise MeshError(f"link to {machine!r} is cut")
        msg = self._ingest_message(message, machine)
        if msg is None:
            raise MeshError("bad message payload")
        mid = msg["id"]
        if mid in mesh.seen_ids or any(
            m.get("id") == mid for m in mesh.provisional
        ):
            return {"id": mid, "duplicate": True}
        sender = mesh.members.get(str(msg.get("from") or ""))
        if sender is not None and sender.machine != machine:
            raise MeshError(
                f"sender {msg.get('from')!r} is not a member of {machine!r}"
            )
        recipients = [
            h for h, m in mesh.members.items()
            if self._is_local(mesh, m) and mesh.addressed_to(msg, h)
        ]
        if not recipients:
            # Nothing for us to inject; the sequenced copy will still arrive
            # through the authority, so this is not an error.
            return {"id": mid, "delivered": []}
        mesh.provisional.append(msg)
        now = time.monotonic()
        for handle in recipients:
            mesh._first_pending.setdefault(handle, now)
        mesh.last_append = now
        mesh.wake.set()
        log.info(
            "mesh %r: fast-path message %s from %r for %s",
            mesh.name, mid, machine, ", ".join(recipients),
        )
        return {"id": mid, "delivered": recipients}

    def peer_sync_accept(
        self,
        name: str,
        machine: str,
        token: str,
        base: int,
        messages: List[dict],
        members: List[dict],
        policy,
        nudges: List[dict],
        peers: Optional[List[str]] = None,
        epoch: Optional[int] = None,
        links: Optional[List[dict]] = None,
        edges: Optional[dict] = None,
        member_edges: Optional[dict] = None,
        roles: Optional[dict] = None,
        lineage: Optional[dict] = None,
    ) -> dict:
        """The authority pushes state at us: log tail, roster, rank order,
        brokered edges, policy, nudges, lineage, and — only when we are
        behind on it — the role set.

        ``base`` must equal our log length — a mismatch means the authority's
        cursor for us is stale, so we answer with ``resync`` and our true
        position instead of applying anything out of order.
        """
        mesh = self._inbound(name, machine, token)
        self._check_primary_token(mesh, machine, token)
        if int(base) != len(mesh.messages):
            return {"resync": len(mesh.messages)}
        now = time.monotonic()
        appended = 0
        for m in messages:
            msg = self._ingest_message(m, machine)
            if msg is None or msg["id"] in mesh.seen_ids:
                continue
            # Fold a fast-path arrival into its authoritative position rather
            # than appending a second copy; whoever already read it keeps a
            # note in delivered_ids so it is not injected twice.
            self._fold_provisional(mesh, msg["id"])
            mesh.messages.append(msg)
            mesh.seen_ids.add(msg["id"])
            self._append_log(mesh, msg)
            appended += 1
            for handle, member in mesh.members.items():
                if self._is_local(mesh, member) and mesh.addressed_to(msg, handle):
                    mesh._first_pending.setdefault(handle, now)
        if peers:
            # Rank order is the authority's to decide; adopting it is what
            # keeps every daemon's view of the graph identical.
            mesh.peers = [str(p) for p in peers if p]
            if mesh.me and mesh.me not in mesh.peers:
                mesh.peers.append(mesh.me)
            if not mesh.primary:
                # This sync promoted us (a handover): take over the duties
                # that only the authority performs. Fanout cursors start at
                # zero and the resync handshake pulls them to the truth.
                self._ensure_pair_links(mesh)
                self._raise_seq_floor(mesh)
                for other in mesh.peers:
                    if other != mesh.me:
                        mesh.link_cursors.setdefault(other, 0)
                log.info(
                    "mesh %r: took over the authority from %r (epoch %s)",
                    mesh.name, machine, epoch,
                )
        if epoch is not None:
            try:
                mesh.authority_epoch = int(epoch)
            except (TypeError, ValueError):
                pass
        if links is not None:
            self._apply_link_grants(mesh, links, sender=machine)
        if isinstance(edges, dict):
            mesh.edges = {str(k): bool(v) for k, v in edges.items()}
        if isinstance(member_edges, dict):
            mesh.member_edges = {
                str(k): bool(v) for k, v in member_edges.items()
            }
        if isinstance(lineage, dict):
            # Adopted wholesale like the roster: the authority is the hub that
            # collects every host's answer. Our own members are re-derived on
            # read, so the copy of ours that comes back here is harmless. A
            # self-parent is dropped rather than trusted — it would be a cycle
            # of one in whatever walks this next.
            mesh.remote_lineage = {
                str(h): str(p) for h, p in lineage.items()
                if p and str(h) != str(p)
            }
        if isinstance(roles, dict):
            # Present only when the authority thinks we are behind. `doc` may
            # legitimately be None — that is the mesh returning to the
            # packaged vocabulary, which is a change like any other.
            mesh.set_roles_doc(
                mesh_roles.load_override(roles.get("doc")),
                version=roles.get("version"),
            )
        if members:
            # The roster is authoritative: adopt it wholesale, keeping local
            # delivery cursors for members that still exist.
            adopted: Dict[str, Member] = {}
            for docm in members:
                if isinstance(docm, dict) and docm.get("handle"):
                    member = Member.from_dict(docm)
                    adopted[member.handle] = member
            for handle in list(mesh.cursors):
                if handle not in adopted:
                    mesh.cursors.pop(handle, None)
                    mesh._first_pending.pop(handle, None)
                    mesh.dismissed.pop(handle, None)
            mesh.members = adopted
            self._persist_def(mesh)
            self._persist_cursors(mesh)
        if policy is not None:
            mesh.policy = mesh_policy.load_policy(policy)
        for nudge in nudges:
            if isinstance(nudge, dict):
                self._apply_nudge_soon(mesh, nudge)
        if appended:
            mesh.last_append = time.monotonic()
            mesh.wake.set()
        mesh.peer_status[machine] = {
            "ok": True, "error": None, "retry_at": 0.0, "backoff": 0.0,
            "last_sync": now,
        }
        return {
            "cursor": len(mesh.messages),
            "activity": self._activity_report(mesh),
            # Lineage rides the ack for the same reason activity does: it is
            # observable only here. Roots are sent as "" rather than omitted,
            # so a member that stopped having a parent clears the authority's
            # copy instead of leaving it frozen at the last thing we said.
            "lineage": {
                h: p or "" for h, p in self._local_lineage(mesh).items()
            },
        }

    def _apply_nudge_soon(self, mesh: Mesh, nudge: dict) -> None:
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.ensure_future(self._apply_nudge(mesh, nudge))

    async def _apply_nudge(self, mesh: Mesh, nudge: dict) -> None:
        """Inject a primary-decided nudge into a local member's PTY.

        Idleness is re-checked at fire time (the primary decided from a
        report that may be stale); a busy member simply drops the nudge —
        the primary's timers re-fire it later.

        Unless it was ``force``d, which marks a nudge an operator pressed a
        button for rather than one a timer decided on. Nothing re-fires that
        one, so dropping it would lose it silently — and the local path never
        checked idleness in the first place, so re-checking here would make
        the same button mean two different things depending on which machine
        happens to host the member. An exited session still refuses: there is
        nothing there to read it.
        """
        handle = str(nudge.get("handle") or "")
        member = mesh.members.get(handle)
        if member is None or not self._is_local(mesh, member):
            return
        try:
            session = self.manager.get(member.session)
        except ManagerError:
            return
        if session.exited:
            return
        if session.status() != STATUS_IDLE and not nudge.get("force"):
            return
        block = mesh_policy.format_nudge(
            mesh.name,
            str(nudge.get("kind") or "nudge"),
            handle,
            str(nudge.get("body") or ""),
        )
        if not await session.deliver(block):
            return
        log.info("mesh %r: applied %s nudge -> %r",
                 mesh.name, nudge.get("kind"), handle)

    def _activity_report(self, mesh: Mesh) -> dict:
        """Observed state of our local members, piggybacked on sync acks so
        the primary's policy engine can reason about remote members."""
        report: dict = {}
        now = time.monotonic()
        for handle, member in mesh.members.items():
            if not self._is_local(mesh, member):
                continue
            try:
                session = self.manager.get(member.session)
            except ManagerError:
                continue
            if session.exited:
                continue
            st = mesh.activity.get(handle) or {}
            last_sent = st.get("last_sent", 0.0)
            last_asked = st.get("last_asked", 0.0)
            unanswered = (
                last_asked > 0
                and last_sent < last_asked
                and bool(mesh.owed(handle))
            )
            pending = len(mesh.pending(handle))
            anchor = st.get("anchor", now)
            active_at = max(last_sent, st.get("last_delivered", 0.0), anchor)
            first_pending = mesh._first_pending.get(handle)
            report[handle] = {
                "idle": session.status() == STATUS_IDLE,
                "delivery_hold": bool(
                    callable(getattr(session, "delivery_held", None))
                    and session.delivery_held()
                ),
                "caught_up": (not unanswered and pending == 0),
                "unanswered": unanswered,
                "pending": pending,
                # How MANY messages are unanswered, not just whether any are:
                # the policy engine only needs the boolean, but the owner's
                # dashboard cannot count a remote member's mail itself (their
                # cursors live here, not there). Ignored by older primaries.
                "owed": len(mesh.owed(handle)),
                "active_ago": max(0.0, now - active_at),
                "first_pending_ago": (
                    max(0.0, now - first_pending)
                    if first_pending is not None else None
                ),
            }
        return report

    # -- flushers -------------------------------------------------------- #
    def _flush_guests_soon(self, mesh: Mesh) -> None:
        """Best-effort immediate fanout to every guest (worker also retries)."""
        if mesh.primary or not mesh.links or self.peer_transport is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _flush_all() -> None:
            for machine in list(mesh.links):
                try:
                    await self._flush_guest(mesh, machine)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "mesh %r: guest flush to %r failed", mesh.name, machine
                    )

        asyncio.ensure_future(_flush_all())

    def _fast_targets(self, mesh: Mesh, recipients: List[str]) -> List[str]:
        """Peer machines we can hand ``recipients``' mail to directly.

        The authority is excluded — a message that took this path is already
        queued for it — as are ourselves and any cut edge.
        """
        out = set()
        for handle in recipients:
            member = mesh.members.get(handle)
            if member is None or self._is_local(mesh, member):
                continue
            machine = member.machine or mesh.authority
            if machine in ("", mesh.me, mesh.authority):
                continue
            if mesh.linked(machine):
                out.add(machine)
        return sorted(out)

    async def _fast_deliver(self, mesh: Mesh, entry: dict) -> None:
        """Push one queued send straight at the peers hosting its recipients.

        Best-effort and idempotent: ``fast_sent`` records who has taken it so
        the worker's retries do not re-push, and the receiving daemon dedupes
        by message id anyway.
        """
        recipients = self._resolve_recipients(
            mesh, str(entry.get("from") or ""), entry.get("to") or "*", strict=False
        )
        done = set(entry.get("fast_sent") or [])
        for machine in self._fast_targets(mesh, recipients):
            if machine in done:
                continue
            try:
                await self._peer_call(
                    mesh, machine, "/peer/mesh/deliver", {"message": entry}
                )
            except PeerUnreachable as exc:
                self._mark_peer_down(mesh, machine, exc)
                continue
            except MeshError as exc:
                # A rejection is final for this edge (cut, stale token): the
                # sequenced copy remains the guaranteed path.
                log.info(
                    "mesh %r: peer %r refused the fast path: %s",
                    mesh.name, machine, exc,
                )
                done.add(machine)
                continue
            done.add(machine)
        if done:
            entry["fast_sent"] = sorted(done)
            self._persist_outbox(mesh)

    async def _flush_fast(self, mesh: Mesh) -> None:
        """Worker duty: retry the fast path for everything still queued."""
        if not mesh.primary or not mesh.outbox or self.peer_transport is None:
            return
        for entry in list(mesh.outbox):
            try:
                await self._fast_deliver(mesh, entry)
            except MeshError:
                continue  # roster moved under us; the outbox drain will tell

    def _flush_upstream_soon(self, mesh: Mesh) -> None:
        if not mesh.primary or self.peer_transport is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.ensure_future(self._flush_upstream(mesh))

    def _mark_peer_down(self, mesh: Mesh, machine: str, exc: Exception) -> None:
        status = mesh.peer_status.setdefault(
            machine, {"ok": None, "error": None, "retry_at": 0.0, "backoff": 0.0}
        )
        now = time.monotonic()
        backoff = min(
            (status.get("backoff") or _PEER_BACKOFF_BASE / 2) * 2,
            _PEER_BACKOFF_MAX,
        )
        status.update(
            {"ok": False, "error": str(exc), "retry_at": now + backoff,
             "backoff": backoff}
        )

    async def _flush_guest(
        self, mesh: Mesh, machine: str, *, force: bool = False,
        urgent: bool = False,
    ) -> bool:
        """Authority → peer sync: log tail + roster + rank + policy + nudges.

        Also fires with an empty payload on a slow cadence while any policy
        is enabled, so the peer's activity report (piggybacked on the ack)
        stays fresh for the policy engine. ``force`` is for the outgoing
        authority's handover push, the one moment a non-authority may send
        this; ``urgent`` additionally ignores the retry backoff, because a
        handover cannot wait for one. Returns whether the peer is in step
        with us — the handover refuses to commit without that.
        """
        if mesh.primary and not force:
            return False
        guest = mesh.links.get(machine)
        if guest is None or self.peer_transport is None:
            return False
        status = mesh.peer_status.setdefault(
            machine, {"ok": None, "error": None, "retry_at": 0.0, "backoff": 0.0,
                      "last_sync": 0.0, "roster_seen": 0}
        )
        now = time.monotonic()
        if now < status.get("retry_at", 0.0) and not urgent:
            return False
        cursor = mesh.link_cursors.get(machine, 0)
        msgs = mesh.messages[cursor:]
        nudges = mesh.pending_nudges.pop(machine, [])
        policy_on = any(
            bool(sec.get("enabled"))
            for sec in mesh.policy.values() if isinstance(sec, dict)
        )
        report_due = policy_on and (
            now - status.get("last_sync", 0.0) >= self.report_interval
        )
        roster_due = status.get("roster_seen", 0) < mesh.roster_version
        # The role set is comparatively fat (stance prose for every role) and
        # changes about never, so unlike the roster it is sent ONLY when this
        # peer is behind on it — never on the back of a message flush.
        roles_due = status.get("roles_seen", -1) != mesh.roles_version
        if (not msgs and not nudges and not roster_due and not report_due
                and not roles_due):
            return True  # already in step
        try:
            resp = await self.peer_transport(
                machine,
                "/peer/mesh/sync",
                {
                    "mesh": mesh.wire_name,
                    "machine": self.machine,
                    "token": guest["token_out"],
                    "base": cursor,
                    "messages": msgs,
                    "members": [m.to_dict() for m in mesh.members.values()],
                    "policy": mesh.policy,
                    "nudges": nudges,
                    # phase 7: rank order + the peer-to-peer edges we broker
                    # for this machine, so the graph converges everywhere.
                    "peers": list(mesh.peers),
                    "epoch": mesh.authority_epoch,
                    "links": self._link_grants_for(mesh, machine),
                    # State of every edge, including ones this peer is not an
                    # endpoint of, so its diagram shows the same graph as ours.
                    "edges": dict(mesh.edges),
                    # The member graph rides the same sync for the same
                    # reason, and for one more: a guest resolves recipients
                    # locally before forwarding, so it has to know the cuts
                    # or it would accept a send the authority then refuses.
                    "member_edges": dict(mesh.member_edges),
                    # Who spawned whom, in handles. Relayed rather than
                    # discovered: only the daemon hosting a session can see
                    # its parent, so this is the one path by which a guest
                    # learns the shape of another guest's team.
                    "lineage": self._lineage_map(mesh),
                    # Only when this peer is behind (see roles_due). Sent as
                    # {doc, version} so a null doc — the mesh going back to the
                    # packaged vocabulary — is distinguishable from "omitted".
                    **({"roles": {"doc": mesh.roles_doc,
                                  "version": mesh.roles_version}}
                       if roles_due else {}),
                },
            )
        except Exception as exc:  # noqa: BLE001 — queue and retry with backoff
            if nudges:
                mesh.pending_nudges[machine] = (
                    nudges + mesh.pending_nudges.get(machine, [])
                )
            self._mark_peer_down(mesh, machine, exc)
            log.info(
                "mesh %r: %d message(s) queued for guest %r (%s); retry in %.0fs",
                mesh.name, len(msgs), machine, exc,
                mesh.peer_status[machine]["backoff"],
            )
            return False
        if not isinstance(resp, dict):
            resp = {}
        if "resync" in resp:
            # Our cursor was stale (state loss on either side): adopt the
            # guest's true position; the next flush sends the real tail.
            try:
                mesh.link_cursors[machine] = max(0, int(resp["resync"]))
            except (TypeError, ValueError):
                pass
            status.update({"ok": True, "error": None, "retry_at": 0.0,
                           "backoff": 0.0, "last_sync": now})
            self._persist_cursors(mesh)
            return True
        mesh.link_cursors[machine] = cursor + len(msgs)
        status.update({"ok": True, "error": None, "retry_at": 0.0,
                       "backoff": 0.0, "last_sync": now,
                       "roster_seen": mesh.roster_version})
        if roles_due:
            # Only after the peer actually took it — a failed sync leaves
            # roles_due true so the next one carries the vocabulary again.
            status["roles_seen"] = mesh.roles_version
        activity = resp.get("activity")
        if isinstance(activity, dict):
            for handle, rep in activity.items():
                member = mesh.members.get(str(handle))
                if (
                    isinstance(rep, dict)
                    and member is not None
                    and not self._is_local(mesh, member)
                ):
                    mesh.remote_activity[str(handle)] = {**rep, "at": now}
        lineage = resp.get("lineage")
        if isinstance(lineage, dict):
            # Per handle rather than wholesale: this guest speaks only for the
            # members it hosts, and overwriting the map would erase what every
            # other guest has told us.
            before = dict(mesh.remote_lineage)
            for handle, parent in lineage.items():
                member = mesh.members.get(str(handle))
                if member is None or self._is_local(mesh, member):
                    continue
                if parent and str(parent) != str(handle):
                    mesh.remote_lineage[str(handle)] = str(parent)
                else:
                    mesh.remote_lineage.pop(str(handle), None)
            if mesh.remote_lineage != before:
                # Lineage arrives one hop later than the roster it describes:
                # the join fans out immediately, but who spawned whom is only
                # learned from the host's next ack. Without marking the others
                # due, that answer would sit here until some unrelated message
                # gave it a lift, and their diagrams would draw a flat team.
                # Bumping the roster is how "a member fact changed" is already
                # said; the worker's next pass does the sending, since we are
                # inside a flush and must not start another.
                mesh.roster_version += 1
        self._persist_cursors(mesh)
        if msgs:
            log.info(
                "mesh %r: synced %d message(s) to guest %r",
                mesh.name, len(msgs), machine,
            )
        return True

    async def _flush_upstream(self, mesh: Mesh) -> None:
        """Mirror → primary: drain the durable outbox strictly in order."""
        if not mesh.primary or not mesh.outbox:
            return
        status = mesh.peer_status.setdefault(
            mesh.primary,
            {"ok": None, "error": None, "retry_at": 0.0, "backoff": 0.0},
        )
        now = time.monotonic()
        if now < status.get("retry_at", 0.0):
            return
        drained = 0
        while mesh.outbox:
            entry = mesh.outbox[0]
            try:
                await self._peer_call_primary(
                    mesh, "/peer/mesh/send", {"message": entry}
                )
            except PeerUnreachable as exc:
                self._mark_peer_down(mesh, mesh.primary, exc)
                self._persist_outbox(mesh)
                log.info(
                    "mesh %r: %d send(s) still queued for primary %r (%s)",
                    mesh.name, len(mesh.outbox), mesh.primary, exc,
                )
                return
            except MeshError as exc:
                # The primary REJECTED it (validation) — dropping is the only
                # honest option; retrying forever would wedge the queue.
                log.warning(
                    "mesh %r: primary rejected queued message %r: %s",
                    mesh.name, entry.get("id"), exc,
                )
                mesh.outbox.pop(0)
                continue
            mesh.outbox.pop(0)
            drained += 1
        self._persist_outbox(mesh)
        status.update({"ok": True, "error": None, "retry_at": 0.0, "backoff": 0.0})
        if drained:
            log.info(
                "mesh %r: forwarded %d queued send(s) to primary %r",
                mesh.name, drained, mesh.primary,
            )

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    #: Un-answered bodies are clipped to this in the owed report — it is a
    #: triage list, and the full text is one click away in the message log.
    OWED_PREVIEW = 240

    def owed_report(
        self, mesh: Mesh, *, state: str = "all",
        handle: Optional[str] = None,
    ) -> dict:
        """Per-member ledger of unanswered mail: who owes what, since when.

        Backs ``claunch mesh owed`` and the web dashboard. Local members are
        read straight off the log (:meth:`Mesh.owed`); remote ones only
        through the activity their own daemon piggybacks on the sync ack,
        which carries counts rather than messages — so their rows are marked
        ``reported`` and the per-message detail lives on that daemon. This is
        the same split ``pending`` has always made, for the same reason: a
        guest owns its members' cursors.

        ``state`` narrows it to one lifecycle partition, the same words
        :func:`mesh_info` takes, and for the same reason: a local row costs a
        walk of the message log twice, so this is members times messages.
        mesh-0826 answered in 4.2s over 251 members and 27708 messages
        (2026-09-20), for a view that polls every 5 seconds and draws the
        live ones. A debt owed by a session that has ended is also the one
        nobody can act on -- there is nothing left to nudge.

        The totals are over the rows this answer contains, so a narrowed
        report reads as the narrowing says; ``member_counts`` is over every
        member, so what was left out is still countable.

        ``handle`` narrows it to one member and overrides ``state``: a caller
        that names a member is answering a question about that member, and a
        row missing because the session has since been killed reads as "no
        such member". It is also the cheap form of the same question -- one
        member's walk instead of every member's.
        """
        now = datetime.now(timezone.utc)
        rows = []
        counts = {
            "all": 0, "current": 0, "running": 0, "remote": 0,
            "killed": 0, "paused": 0, "archived": 0, "missing": 0,
        }
        for h in sorted(mesh.members):
            member = mesh.members[h]
            category = self._member_category(mesh, member)
            counts["all"] += 1
            if category in counts:
                counts[category] += 1
            if category in ("running", "remote"):
                counts["current"] += 1
            if handle is not None:
                if h != handle:
                    continue
            elif not member_in_state(category, state):
                continue
            local = self._is_local(mesh, member)
            row: dict = {
                "handle": h,
                "role": member.role,
                "subroles": list(member.subroles),
                "roles": member.roles,
                # Blank means the authority's own member, not ours (v2).
                "machine": member.machine or mesh.authority or "",
                "session": member.session,
                "local": local,
                "reachability": self._reachability(mesh, member),
                "source": "log" if local else "reported",
                "messages": [],
                "owed": None,
                "pending": None,
                "oldest_age": None,
                "stale": False,
                # What this daemon can actually do about the row, decided
                # here rather than re-derived by every reader: a nudge needs
                # either the session (local) or the authority's channel to
                # the daemon that has it, and a dismissal needs the log the
                # debt is read from, which only the member's own daemon has.
                "can_nudge": local or not mesh.primary,
                "can_dismiss": local,
            }
            if local:
                owed = mesh.owed(h)
                row["pending"] = len(mesh.pending(h))
                row["owed"] = len(owed)
                for m in owed:
                    body = recipient_body(m, h)
                    row["messages"].append(
                        {
                            "id": m.get("id"),
                            "from": m.get("from"),
                            "type": msg_type_for(m, h),
                            "ts": m.get("ts"),
                            "age": _age_secs(m.get("ts"), now),
                            "reply_to": m.get("reply_to"),
                            "batch": m.get("sections") is not None,
                            "body": (
                                body[: self.OWED_PREVIEW] + " …"
                                if len(body) > self.OWED_PREVIEW else body
                            ),
                        }
                    )
                ages = [e["age"] for e in row["messages"] if e["age"] is not None]
                row["oldest_age"] = max(ages) if ages else None
            else:
                rep = mesh.remote_activity.get(h)
                if isinstance(rep, dict):
                    row["pending"] = rep.get("pending")
                    # 'owed' is only present from a daemon new enough to count
                    # it; older guests still report the unanswered boolean, so
                    # fall back to that rather than showing nothing.
                    if rep.get("owed") is not None:
                        row["owed"] = int(rep["owed"])
                    elif rep.get("unanswered") is not None:
                        row["owed"] = 1 if rep["unanswered"] else 0
                    reported_at = float(rep.get("at") or 0.0)
                    row["stale"] = (
                        time.monotonic() - reported_at
                        > max(15.0, 3 * self.report_interval)
                    )
            rows.append(row)
        return {
            "mesh": mesh.name,
            "at": utcnow(),
            "members": rows,
            "member_counts": counts,
            "member_state": "one" if handle is not None else state,
            "member_handle": handle,
            "owed": sum(r["owed"] or 0 for r in rows),
            "pending": sum(r["pending"] or 0 for r in rows),
            "owing": sum(1 for r in rows if (r["owed"] or 0) > 0),
            # The nudger only chases members whose session is idle, and only
            # on the mesh's own daemon — a mirror's dashboard can show a debt
            # nothing here will act on, so say whose engine is in charge.
            "engine": mesh.primary or mesh.me or None,
            "heartbeat": dict(mesh.policy["heartbeat"]),
        }

    # ------------------------------------------------------------------ #
    # acting on unanswered mail
    #
    # The ledger above is a reading; these two are the operator's answers to
    # it, and they are deliberately the only two. Either the member is asked
    # again (nudge — the heartbeat's move, made by hand and now), or the debt
    # is written off (dismiss — the one closure that is not a reply). Both
    # keep the dashboard and the nudger saying the same thing afterwards,
    # which is the single rule this whole feature is built on.
    # ------------------------------------------------------------------ #
    async def nudge(self, name: str, handle: str, body: str = "") -> dict:
        """Nudge ``handle`` about its unanswered mail, right now.

        The heartbeat with the waiting taken out: same injected block, same
        transport (inject locally, queue for the guest daemon otherwise), but
        fired by an operator who is looking at the debt rather than by a timer
        that has just come round. Unlike the heartbeat it does **not** check
        idleness or whether anything is actually owed — a human clicking
        'nudge' next to a row has already made both judgements, and refusing
        because the member is mid-turn would make the button unreliable in
        exactly the case it is reached for. That holds across the federation:
        the queued form carries ``force``, so the member's own daemon does not
        apply the idleness re-check it applies to the engine's nudges (which
        it can drop safely, because the engine re-fires them and nothing
        re-fires this one).

        It does reset the automatic heartbeat's next fire, though: a member
        that was just poked by hand should not be poked again by the engine a
        second later, having had no chance to answer either.
        """
        mesh = self.get(name)
        member = mesh.members.get(handle)
        if member is None:
            raise MeshError(f"no member {handle!r} in mesh {name!r}")
        local = self._is_local(mesh, member)
        if not local and mesh.primary:
            # Only the authority holds a channel to the member's own daemon;
            # a mirror queueing a nudge would queue it into a fanout it does
            # not perform, and it would sit there forever.
            raise MeshError(
                f"{handle!r} runs on {member.machine or '?'} — nudge it from "
                f"the primary daemon ({mesh.primary})"
            )
        session = None
        if local:
            try:
                session = self.manager.get(member.session)
            except ManagerError:
                raise MeshError(
                    f"{handle!r}'s session {member.session!r} is gone"
                ) from None
            if session.exited:
                raise MeshError(
                    f"{handle!r}'s session {member.session!r} has exited"
                )
        text = (
            " ".join(str(body or "").split())[:500]
            or mesh.policy["heartbeat"]["body"]
        )
        if not await mesh_policy.dispatch(
            self, mesh, member, session, "nudge", handle, text, force=True
        ):
            # The heartbeat can shrug this off and try again in a minute; a
            # button cannot. A terminal that will not take a message is the
            # answer the operator came for, so it is reported, not swallowed.
            raise MeshError(
                f"{handle!r}'s terminal did not take the nudge — its session "
                "may still be starting, or its agent may be mid-write"
            )
        st = mesh.activity.setdefault(handle, {"anchor": time.monotonic()})
        hb = mesh.policy["heartbeat"]
        st["hb_backoff"] = hb["interval"]
        st["hb_next"] = time.monotonic() + hb["interval"]
        log.info("mesh %r: manual nudge -> %r", mesh.name, handle)
        return {
            "mesh": mesh.name,
            "handle": handle,
            "body": text,
            # Locally it is already in the terminal; for a remote member it is
            # an instruction its daemon will carry out on its next sync.
            "queued": not local,
            "owed": len(mesh.owed(handle)) if local else None,
        }

    def dismiss_owed(
        self, name: str, handle: str, ids: Optional[Iterable[str]] = None
    ) -> dict:
        """Write off unanswered mail for ``handle`` — one message, or all.

        ``ids`` None dismisses everything currently owed; otherwise the ids
        given, which must be either owed right now or dismissed already. A
        second press on a row a poll has not yet redrawn is therefore a
        no-op rather than an error, while an id this member never owed is
        refused: it would install a suppression nothing could ever clear,
        and a mistyped id would otherwise 'succeed' having done nothing.

        Afterwards the heartbeat is settled the same way a cut member edge
        settles it (:meth:`_mark_member_edge`): a member left owing nothing
        has ``last_asked`` cleared, so the engine stops chasing a debt the
        operator has just declared closed. Without that the button would be a
        lie — the row would go away and the nudges would keep arriving.
        """
        mesh = self.get(name)
        member = mesh.members.get(handle)
        if member is None:
            raise MeshError(f"no member {handle!r} in mesh {name!r}")
        if not self._is_local(mesh, member):
            # Their daemon owns the cursor, so it owns what is owed against
            # it; we only ever hear the count. Same split as `pending`.
            raise MeshError(
                f"{handle!r} runs on {member.machine or '?'} — its unanswered "
                "mail is counted there, so dismiss it on that daemon"
            )
        owed = {m.get("id") for m in mesh.owed(handle) if m.get("id")}
        already = mesh.dismissed.get(handle) or frozenset()
        if ids is None:
            targets = set(owed)
        else:
            wanted = {str(i) for i in ids}
            targets = wanted & owed
            missing = sorted(wanted - owed - already)
            if missing:
                raise MeshError(
                    f"{handle!r} does not owe an answer to: {', '.join(missing)}"
                )
        dropped = mesh.dismissed.setdefault(handle, set())
        dropped |= targets
        # Prune against the live window rather than letting the set grow with
        # the log: once the member speaks, everything behind falls out of
        # `owed_all` and no id in here is suppressing anything.
        live = {m.get("id") for m in mesh.owed_all(handle)}
        dropped &= live
        if dropped:
            mesh.dismissed[handle] = dropped
        else:
            mesh.dismissed.pop(handle, None)
        remaining = mesh.owed(handle)
        st = mesh.activity.get(handle)
        if st and st.get("last_asked") and not remaining:
            st["last_asked"] = 0.0
        self._persist_cursors(mesh)
        log.info(
            "mesh %r: dismissed %d unanswered message(s) for %r (%d left)",
            mesh.name, len(targets), handle, len(remaining),
        )
        return {
            "mesh": mesh.name,
            "handle": handle,
            "dismissed": sorted(targets),
            "owed": len(remaining),
        }

    # ------------------------------------------------------------------ #
    # lineage
    #
    # Who spawned whom, expressed in handles. The session tree is the only
    # source (``SessionDef.parent``); these two just carry it to the layer
    # that draws it, and no further — nothing routes or authorises on it.
    # ------------------------------------------------------------------ #
    def _local_lineage(self, mesh: Mesh) -> Dict[str, Optional[str]]:
        """Parent handle for each member this daemon hosts, or None for a root.

        The tree is a tree of *sessions* and the roster is a list of
        *members*, and the two need not line up: a session in the middle of a
        lineage may never have been enrolled. So a member's parent is its
        nearest *enrolled* ancestor rather than its immediate one — a worker
        whose lead never joined hangs off whoever above it did, and off
        nothing if nobody did. Collapsing rather than breaking is what keeps
        the drawn result a tree instead of a scatter of orphans.

        ``SessionManager.ancestors`` walks nearest-first, stops at the first
        parent that no longer exists (a dangling parent makes its child a
        root) and is cycle-guarded, so all three of those cases arrive here
        already answered.
        """
        by_session = {
            m.session: h for h, m in mesh.members.items()
            if self._is_local(mesh, m) and m.session
        }
        out: Dict[str, Optional[str]] = {}
        for handle, member in mesh.members.items():
            if not self._is_local(mesh, member) or not member.session:
                continue
            out[handle] = next(
                (
                    by_session[name]
                    for name in self.manager.ancestors(member.session)
                    if by_session.get(name, handle) != handle
                ),
                None,
            )
        return out

    def _lineage_map(self, mesh: Mesh) -> Dict[str, str]:
        """Every member's parent, ours derived and the rest as reported.

        The authority relays this the way it relays the roster, and for the
        same reason it relays the edge table: lineage is knowable only where
        the session runs, so without a hub every dashboard but the host's
        would draw a remote machine's agents as a flat pile.
        """
        out = dict(mesh.remote_lineage)
        for handle, parent in self._local_lineage(mesh).items():
            if parent:
                out[handle] = parent
            else:
                out.pop(handle, None)  # became a root; say so, don't go quiet
        return {
            h: p for h, p in out.items()
            if h in mesh.members and p in mesh.members
        }

    def mesh_rail_info(self, mesh: Mesh) -> dict:
        """The dashboard rail's compact mesh summary.

        The rail needs local session memberships to label its rows and a few
        counts for the sidebar. Topology, peer state, and member-link tables
        are served by the selected mesh's detail request.
        """
        members = [
            {
                "handle": member.handle,
                "session": member.session,
                "role": member.role,
                "subroles": list(member.subroles),
                "roles": member.roles,
                "local": True,
            }
            for _handle, member in sorted(mesh.members.items())
            if self._is_local(mesh, member)
        ]
        return {
            "name": mesh.name,
            "address": self.address(mesh),
            "project": mesh.project or projects.DEFAULT,
            "primary": mesh.primary or None,
            "members": members,
            "member_count": len(mesh.members),
            "messages": len(mesh.messages),
            "requests": len(mesh.pending_requests) if not mesh.primary else 0,
            "visibility": mesh.visibility if not mesh.primary else None,
        }

    def mesh_info(
        self, mesh: Mesh, *, session: str = "", state: str = "all"
    ) -> dict:
        """One mesh in full. ``state`` selects which members get a record.

        The roster is the expensive half of this answer, and almost all of it
        is usually hidden: the page's default filter shows running and remote
        members, and a long-lived mesh is mostly ended ones. Building what the
        reader cannot see is not free -- ``pending`` and ``owed`` walk the
        message log per member, so the cost is members times messages. On
        mesh-0826 (251 members, 27708 messages) that was 4.1s per request,
        against a view that polls every 5 seconds (measured 2026-09-20).

        So the filter is applied here rather than in the reader. The counts
        are taken over every member either way -- a filter bar that cannot
        say what it is hiding is worse than no filter -- and ``state="all"``
        is the default, so a caller that says nothing gets what it always
        got.
        """
        members = []
        lineage = self._local_lineage(mesh)
        counts = {
            "all": 0, "current": 0, "running": 0, "remote": 0,
            "killed": 0, "paused": 0, "archived": 0, "missing": 0,
        }
        for handle in sorted(mesh.members):
            m = mesh.members[handle]
            category = self._member_category(mesh, m)
            counts["all"] += 1
            if category in counts:
                counts[category] += 1
            if category in ("running", "remote"):
                counts["current"] += 1
            if not member_in_state(category, state):
                continue
            local = self._is_local(mesh, m)
            # Unanswered mail alongside undelivered: 'pending' is the daemon's
            # debt to the member, 'owed' the member's debt to the mesh. Remote
            # members are counted from their own daemon's activity report.
            rep = mesh.remote_activity.get(handle) or {}
            if local:
                owed = len(mesh.owed(handle))
            elif rep.get("owed") is not None:
                owed = int(rep["owed"])
            elif rep.get("unanswered") is not None:
                owed = 1 if rep["unanswered"] else 0
            else:
                owed = None
            # Ours is derived on the spot; everyone else's is what their
            # daemon last told us. A parent that has since left reads as no
            # parent at all — the same rule the session tree uses for a
            # dangling one, so a departure cannot leave an edge pointing at
            # nobody.
            parent = lineage.get(handle) if local else mesh.remote_lineage.get(handle)
            members.append(
                {
                    **m.to_dict(),
                    # Stated, not left to be re-derived from `machine`: the
                    # blank-machine test a reader would reach for is the one
                    # `is_local_member` exists to stop them writing. Same key
                    # `owed_report` already ships, for the same reason.
                    "local": local,
                    "pending": len(mesh.pending(handle)) if local else None,
                    "owed": owed,
                    "reachability": self._reachability(mesh, m),
                    # The lifecycle partition the roster filters by: the same
                    # four words the session rail uses, plus the two a member
                    # can be in and a session cannot (``missing``, ``remote``
                    # — see `_member_category`). `reachability` stays exactly
                    # as it was: the CLI prints it and the spawn tests read it.
                    "category": category,
                    "parent": parent if parent in mesh.members else None,
                }
            )
        # The whole rank list, ourselves included — the diagram draws nodes
        # from this and edges from `links`, so both must be absolute.
        peers = []
        for rank, machine in enumerate(mesh.peers):
            status = mesh.peer_status.get(machine) or {}
            link = mesh.links.get(machine)
            mine = bool(mesh.me) and machine == mesh.me
            peers.append(
                {
                    "machine": machine,
                    "rank": rank,
                    "role": "authority" if rank == 0 else "peer",
                    "self": mine,
                    "linked": link is not None,
                    "enabled": bool((link or {}).get("enabled", True)),
                    "owns_link": mesh.owns_link(machine),
                    "linked_at": (link or {}).get("created_at", ""),
                    "ok": None if mine else status.get("ok"),
                    "error": None if mine else status.get("error"),
                    # Toward the authority we queue unsequenced sends; toward
                    # anyone else the log tail itself is the queue.
                    "queued": (
                        0 if mine
                        else len(mesh.outbox) if machine == mesh.authority
                        else max(
                            0,
                            len(mesh.messages) - mesh.link_cursors.get(machine, 0),
                        )
                    ),
                    "members": sorted(
                        h for h, m in mesh.members.items()
                        # A blank machine is the AUTHORITY's own member (v2),
                        # which is us only while we hold authority — bucketing
                        # it under `me` on a mirror hands the authority's
                        # agents to whoever is reading, and leaves the
                        # authority's own cluster drawn empty. On a mesh that
                        # never federated `authority` is `me`, so the
                        # never-federated case is unchanged.
                        if (m.machine or mesh.authority) == machine
                    ),
                }
            )
        requests = []
        if not mesh.primary:
            for r in sorted(
                mesh.pending_requests.values(),
                key=lambda r: r.get("requested_at", ""),
            ):
                requests.append(
                    {
                        k: r.get(k)
                        for k in ("id", "machine", "session", "handle", "role",
                                  "requested_at")
                    } | {"attach": not r.get("session")}
                )
        # Who the asking session is, resolved here rather than guessed by the
        # reader. `None` is a real answer — "this daemon has no member for
        # that session" — and is what a caller must report; it is not the
        # same as not having asked, which is what an absent `session` gets.
        you = self.member_for_session(mesh, session) if session else None
        return {
            "name": mesh.name,
            "address": self.address(mesh),
            "wire_name": mesh.wire_name,
            "origin": mesh.origin or None,
            "project": mesh.project or projects.DEFAULT,
            "created_at": mesh.created_at,
            "primary": mesh.primary or None,
            "authority": mesh.authority or None,
            "self": mesh.me or None,
            "epoch": mesh.authority_epoch,
            "you": you.handle if you else None,
            "members": members,
            # Over every member, not the shown ones: what the bar is hiding
            # is exactly what these are for.
            "member_counts": counts,
            "member_state": state,
            "messages": len(mesh.messages),
            "provisional": len(mesh.provisional),
            "peers": peers,
            "links": self.edge_table(mesh),
            # The member graph, one layer up from `links`: who may message
            # whom. Every pair is listed with its state — see
            # Mesh.member_edge_table on why the cut set alone is not enough.
            "member_links": mesh.member_edge_table(
                None if state == "all" else [m["handle"] for m in members]
            ),
            "requests": requests,
            # Who may discover it: owner side only (a mirror publishes
            # nothing — discovery is one hop from the owner).
            "visibility": mesh.visibility if not mesh.primary else None,
            "offers": sorted(mesh.offers) if not mesh.primary else [],
            "policy": mesh.policy,
            # A summary only — the stance prose is fetched on demand from
            # /api/mesh/<name>/roles, so the 2s dashboard poll stays cheap.
            "roles": {
                "version": mesh.roles_version,
                "custom": mesh.roles_doc is not None,
                "default": mesh.roleset.default,
                "names": sorted(mesh.roleset.roles),
                "is_authority": not mesh.primary,
            },
        }

    def _reachability(self, mesh: Mesh, member: Member) -> str:
        if not self._is_local(mesh, member):
            return (
                "remote-connected" if self.relay_connected() else "remote-disconnected"
            )
        try:
            session = self.manager.get(member.session)
        except ManagerError:
            return "missing"
        return "exited" if session.exited else session.status()

    def member_category(self, mesh: Mesh, member: Member) -> str:
        """Which lifecycle partition a member is in — the public name.

        The routes that narrow a roster by ``state`` ask this; the word and
        the rule are :meth:`_member_category`'s, so there is one definition
        of what "killed" means whoever is asking.
        """
        return self._member_category(mesh, member)

    def _member_category(self, mesh: Mesh, member: Member) -> str:
        """Which lifecycle partition a member is in, for the roster's filter.

        A local member gets the same answer the session rail gets, from the
        same function (:func:`session.session_category`), so one record cannot
        be filed as killed on one page and archived on another.

        The other two words are the cases a session cannot be in, and they are
        not guesses. ``missing`` means the record is gone entirely. ``remote``
        means another daemon's member: our copy of its liveness comes from the
        activity report, which skips exited sessions, so a dead remote member
        is indistinguishable from a live one here. Calling it dead would hide
        a member that may be working, so a filter that drops killed records
        keeps ``remote`` — the roster would otherwise be least trustworthy
        exactly where it is least able to check.
        """
        if not self._is_local(mesh, member):
            return "remote"
        try:
            session = self.manager.get(member.session)
        except ManagerError:
            return "missing"
        return session_category(session)

    # ------------------------------------------------------------------ #
    # delivery worker
    # ------------------------------------------------------------------ #
    def _ensure_worker(self, name: str) -> None:
        if not self._started or name in self._workers:
            return
        self._workers[name] = asyncio.ensure_future(self._worker(self._meshes[name]))

    async def _worker(self, mesh: Mesh) -> None:
        try:
            while True:
                try:
                    await asyncio.wait_for(mesh.wake.wait(), timeout=_POLL)
                    mesh.wake.clear()
                except asyncio.TimeoutError:
                    pass
                if time.monotonic() - mesh.last_append < self.settle:
                    continue  # burst still settling; coalesce
                for handle in list(mesh.members):
                    member = mesh.members.get(handle)
                    if member is None or not self._is_local(mesh, member):
                        continue
                    try:
                        await self._deliver_to(mesh, member)
                    except Exception:  # noqa: BLE001 — one member must not stall the mesh
                        log.exception(
                            "mesh %r: delivery to %r failed", mesh.name, handle
                        )
                if mesh.rename_notice and self.peer_transport is not None:
                    try:
                        await self._flush_rename_notice(mesh)
                    except Exception:  # noqa: BLE001
                        log.exception("mesh %r: rename notice failed", mesh.name)
                if mesh.primary:
                    # Peer duties: drain the outbox toward the authority and,
                    # while that is stuck, keep retrying the direct pushes.
                    # The policy engine deliberately does NOT run here — it
                    # lives on the authority.
                    if self.peer_transport is not None:
                        try:
                            await self._flush_upstream(mesh)
                        except Exception:  # noqa: BLE001
                            log.exception(
                                "mesh %r: upstream flush failed", mesh.name
                            )
                        try:
                            await self._flush_fast(mesh)
                        except Exception:  # noqa: BLE001
                            log.exception(
                                "mesh %r: fast-path flush failed", mesh.name
                            )
                    continue
                if self.peer_transport is not None:
                    for machine in list(mesh.links):
                        try:
                            await self._flush_guest(mesh, machine)
                        except Exception:  # noqa: BLE001
                            log.exception(
                                "mesh %r: guest flush to %r failed",
                                mesh.name, machine,
                            )
                    try:
                        await self._flush_grants(mesh)
                    except Exception:  # noqa: BLE001
                        log.exception(
                            "mesh %r: grant flush failed", mesh.name
                        )
                try:
                    self._response_watch_tick(mesh)
                except Exception:  # noqa: BLE001 -- sender notices must not stall delivery
                    log.exception("mesh %r: response watch tick failed", mesh.name)
                try:
                    await mesh_policy.tick(self, mesh)
                except Exception:  # noqa: BLE001 — a policy bug must not stall delivery
                    log.exception("mesh %r: policy tick failed", mesh.name)
        except asyncio.CancelledError:
            pass

    async def _report_stranded(
        self, mesh: Mesh, member: Member, pending: List[dict], state: str
    ) -> None:
        """Tell the senders of ``pending`` that ``member`` cannot read them.

        The send-time notice (:func:`stranded_notice`) covers the sender who
        is standing right there; this covers the other order of events — the
        message was accepted into a live terminal and the terminal died
        before delivery. Nobody is holding a result to read in that case, so
        the daemon has to go and say it.

        Exactly one report per death PER SENDER. The delivery worker runs
        every few seconds and a stranded backlog never drains on its own, so
        anything less than a latch is a message every tick forever — but a
        latch on the dead member alone told the first sender and left every
        later one in the silence this exists to break. So the latch records
        WHO has been told, and a sender not in it is told once. The record
        persists with delivery cursors across daemon restarts and clears
        when the session comes back (see :meth:`_deliver_to`), which makes
        a second death reportable. Only explicitly addressed messages count;
        wildcard broadcasts remain queued without generating these reports.

        Sent as ``fyi`` from the policy handle, like a stall warning: the
        senders are being told something, not asked for anything, and an
        answer here would only be owed back to a daemon.
        """
        told = mesh.stranded_told.get(member.handle, [])
        # General broadcasts do not establish a delivery obligation to each
        # absent member. Keep their mail queued, but do not notify the author.
        pending = [m for m in pending if m.get("to") != "*"]
        # Only members can be messaged back. An external sender (the operator
        # at a dashboard, or the policy engine itself) has no terminal in this
        # mesh, and a self-report would be a daemon talking to itself.
        senders = sorted(
            {
                str(m.get("from") or "")
                for m in pending
                if str(m.get("from") or "") in mesh.members
                and str(m.get("from") or "") != member.handle
            }
        )
        fresh = [s for s in senders if s not in told]
        if not fresh:
            return
        held = stranded_notice(
            [{"handle": member.handle, "session": member.session, "state": state}]
        )
        for sender in fresh:
            waiting = sum(m.get("from") == sender for m in pending)
            body = (
                f"{member.handle} is not reading you: {held} "
                f"{waiting} message(s) of yours are waiting there."
            )
            try:
                self._send_core(mesh, mesh_policy.POLICY_SENDER, [sender], body,
                                external=True, type="fyi")
            except MeshError as exc:
                log.debug("mesh %r: stranded report failed: %s", mesh.name, exc)
                continue
            told = [*told, sender]
            mesh.stranded_told[member.handle] = told
            self._persist_cursors(mesh)
            self._flush_guests_soon(mesh)
            log.info(
                "mesh %r: told %s that %r (session %r) is %s with %d message(s) held",
                mesh.name, sender, member.handle, member.session, state, waiting,
            )

    def _delivery_origins(self, mesh: Mesh, msgs: List[dict]) -> Dict[str, str]:
        """Which daemon each sender in this batch speaks from, "" for ours.

        The block is composed on the daemon the RECIPIENT runs on, so asking
        :meth:`_is_local` about the SENDER already answers "same daemon as the
        reader". That answer is worth carrying because it decides what the
        reader may assume about a peer before it replies: a member on this
        daemon shares the filesystem, the git objects and the board, and one
        on another daemon shares only the relay, which is the difference
        between reading its checkout directly and going through ``peer_file``.
        A sender with no member row is an external send — the operator at the
        dashboard — and is left out rather than guessed at.
        """
        out: Dict[str, str] = {}
        for m in msgs:
            handle = str(m.get("from") or "")
            if not handle or handle in out:
                continue
            member = mesh.members.get(handle)
            if member is None:
                continue
            out[handle] = (
                "" if self._is_local(mesh, member)
                else (member.machine or "?")
            )
        return out

    async def _deliver_to(
        self, mesh: Mesh, member: Member, *, force: bool = False
    ) -> None:
        try:
            session: Optional[AnySession] = self.manager.get(member.session)
        except ManagerError:
            session = None  # removed; hold the cursor, deliver on rejoin/respawn
        gone = session is None or session.exited
        log_shape = (len(mesh.messages), len(mesh.provisional))
        if gone and not force and mesh._stranded_scan.get(member.handle) == log_shape:
            return  # nothing appended since the last look at this dead member
        pending = mesh.pending(member.handle)
        if not pending:
            # This scan has established that nothing in the log up to here is
            # for this member, and nothing turns into a pending message for it
            # later: a member joins already caught up (``join`` stamps its
            # cursor at the end of the log), so a backlog is never replayed
            # for it, not even when its edges change. Leaving the cursor
            # behind meant every pass re-walked the same messages, for every
            # member, for as long as nobody addressed it -- which on a mesh
            # with hundreds of members is most of them. The cursor is not
            # persisted here: it is an optimisation, and a daemon that reads
            # the older number back simply re-walks once.
            mesh.cursors[member.handle] = len(mesh.messages)
        if gone:
            if pending:
                # Hold until respawn (same name, same cursor) — and tell
                # whoever is waiting on this member, ONCE. Holding is right;
                # holding in silence is what lets a sender spend its next ten
                # turns talking to a terminal that closed an hour ago.
                await self._report_stranded(
                    mesh, member, pending,
                    "missing" if session is None else "exited",
                )
            else:
                mesh._first_pending.pop(member.handle, None)
            # Stamped AFTER the report: the report is itself an append, and
            # counting it would buy exactly one more full rescan per death.
            mesh._stranded_scan[member.handle] = (
                len(mesh.messages), len(mesh.provisional)
            )
            return
        mesh._stranded_scan.pop(member.handle, None)
        # Seeing a live recipient re-arms warnings even when its queue is empty.
        if mesh.stranded_told.pop(member.handle, None) is not None:
            self._persist_cursors(mesh)
        if not pending:
            mesh._first_pending.pop(member.handle, None)
            return
        assert session is not None
        # ``force`` is a human at the dashboard saying "type it in now" (see
        # :meth:`flush_session`). It drops THIS gate — the gate exists to keep
        # an automated paste out of a running turn, and waiting that out is
        # exactly what the operator is declining to do — and it is carried
        # into :meth:`Session.deliver` as well, because a button that answers
        # "still waiting" is a button that did nothing. What it buys there is
        # narrow and stated in that docstring: a short keyboard wait instead
        # of a long one, and an unsent line submitted ahead of the delivery
        # rather than the delivery refusing to land behind it. The paste is
        # still assembled the same way; nothing about arriving intact is
        # skipped, because no impatience makes that safe.
        #
        # A live keyboard is held exactly like a running turn: the human is
        # mid-composition, and their thinking pauses outlast the idle
        # threshold, so the screen alone would call this moment deliverable.
        #
        # A hold a PERSON set is checked first and separately, because it is
        # the one hold with no timeout: the gate below gives up after
        # ``busy_hold`` and types in anyway, which is right for a guess drawn
        # from timing and wrong for somebody who said "not into this
        # terminal". ``force`` still wins — that is the same person at the
        # same dashboard pressing "deliver now", and a hold you can no longer
        # get out of is a trap, not a setting.
        if not force and session.delivery_held():
            return  # held by a human until they say otherwise
        if not force and (
            session.status() != STATUS_IDLE or session.keyboard_busy()
        ):
            held = time.monotonic() - mesh._first_pending.get(
                member.handle, time.monotonic()
            )
            if held < self.busy_hold:
                return  # idle-gate: don't interleave with a running turn
        # Pacing, and deliberately LAST of the automatic gates: it is the one
        # that still binds after the idle-gate has given up and decided to
        # type into a running turn. That is the whole point — ``busy_hold``
        # bounds how long one message waits, and nothing before this bounded
        # how OFTEN a terminal is written to. Everything pending goes in one
        # block, so the wait is never lost work: it is the next burst
        # coalescing into the block after this one instead of arriving as
        # three separate interruptions.
        #
        # ``force`` drops it like every other automatic gate — the operator
        # pressing "deliver now" is declining exactly this wait.
        if not force:
            gap = self.paced_for(mesh, member.handle)
            if gap > 0:
                return  # paced: the last delivery into this terminal is recent
        block = format_delivery(
            mesh.name, member.handle, pending,
            origins=self._delivery_origins(mesh, pending),
        )
        if not await session.deliver(block, force=force):
            return  # undelivered: hold the cursor, the next tick retries
        mesh.cursors[member.handle] = len(mesh.messages)
        # Fast-path arrivals are not in the log yet, so the cursor cannot
        # cover them: remember their ids until the sequenced copy has been
        # folded in behind the cursor.
        delivered = set(mesh.delivered_ids.get(member.handle) or ())
        delivered.update(m["id"] for m in pending if m.get("id"))
        ahead = {m.get("id") for m in mesh.messages[mesh.cursors[member.handle]:]}
        ahead.update(m.get("id") for m in mesh.provisional)
        mesh.delivered_ids[member.handle] = delivered & ahead
        mesh._first_pending.pop(member.handle, None)
        self._watch_delivered_responses(mesh, member.handle, pending)
        now = time.monotonic()
        st = mesh.activity.setdefault(member.handle, {"anchor": now})
        st["last_delivered"] = now
        # Only a reply-*expecting* delivery arms the heartbeat: draining
        # fyi/ack traffic leaves the member owing nothing (interconnect's
        # expects_reply contract). Sectioned messages count per recipient.
        if any(expects_reply(msg_type_for(m, member.handle)) for m in pending):
            st["last_asked"] = now
        self._persist_cursors(mesh)
        log.info(
            "mesh %r: delivered %d message(s) to %r (session %r)",
            mesh.name, len(pending), member.handle, member.session,
        )

    # ------------------------------------------------------------------ #
    # persistence
    # ------------------------------------------------------------------ #
    def _migrate_v2(self, mesh: Mesh, doc: Optional[dict] = None) -> None:
        """Fold retired primary/mirror state into the rank list (phase 7).

        An owner becomes rank 0 with its guests following in link order; a
        mirror keeps its primary at rank 0 and appends itself. ``load_all``
        runs before the uplink resolves our relay name, so this is called
        again from the ``machine`` setter — until then the v2 fragment just
        waits on the mesh.
        """
        if doc is not None:
            guests = doc.get("guests")
            link = doc.get("link")
            mesh._v2 = {
                "primary": str(doc.get("primary") or ""),
                "link": dict(link) if isinstance(link, dict) else None,
                "guests": {
                    str(k): dict(v) for k, v in (guests or {}).items()
                    if isinstance(v, dict)
                },
            }
        v2 = mesh._v2
        if not v2 or mesh.peers:
            return
        primary, link, guests = v2["primary"], v2["link"], v2["guests"]
        if primary:
            if link is None:
                log.warning(
                    "mesh %r: mirror of %r has no link credentials — unlinked",
                    mesh.name, primary,
                )
                mesh._v2 = None
                return
            if not mesh.me:
                return  # retry once the relay name lands
            peers = [primary, mesh.me]
            links = {primary: {**link, "enabled": True}}
        elif guests:
            if not mesh.me:
                return
            ordered = sorted(
                guests, key=lambda m: str(guests[m].get("created_at") or "")
            )
            peers = [mesh.me, *ordered]
            links = {m: {**guests[m], "enabled": True} for m in ordered}
        else:
            mesh._v2 = None  # purely local mesh: nothing to migrate
            return
        mesh.peers = peers
        for machine, link_doc in links.items():
            mesh.links.setdefault(machine, link_doc)
        # After the assignment above, and must not be moved before it: until
        # then `peers` is empty (this runs only when it is — see the guard at
        # the top), so `authority` would fall back to `mesh.me` and the
        # mirror branch would claim the primary's rows as ours.
        self._absolutize_roster(mesh)
        mesh._v2 = None
        log.info(
            "mesh %r: migrated federation state to rank order %s",
            mesh.name, mesh.peers,
        )
        self._persist_def(mesh)

    def _load(self, d: Path) -> Mesh:
        doc = json.loads((d / "mesh.json").read_text(encoding="utf-8"))
        mesh = Mesh(
            str(doc["name"]),
            created_at=str(doc.get("created_at") or ""),
            # The uplink resolves our relay name after load_all, so fall back
            # to the identity this directory was last written with — that is
            # what makes rank (and therefore `primary`) correct on reload.
            me=self.machine or str(doc.get("self") or ""),
            project=str(doc.get("project") or ""),
        )
        for entry in (doc.get("members") or {}).values():
            member = Member.from_dict(entry)
            mesh.members[member.handle] = member
        # Phase 7 always writes "peers" beside "links". A doc carrying
        # "links" *without* "peers" is retired v1 symmetric-federation
        # state, which is dropped rather than migrated (as it has been
        # since v2) — the two formats happen to share the key name.
        mesh.peers = [str(p) for p in (doc.get("peers") or []) if p]
        if mesh.peers:
            mesh.links = {
                str(k): dict(v) for k, v in (doc.get("links") or {}).items()
                if isinstance(v, dict)
            }
            mesh.pair_links = {
                str(k): dict(v)
                for k, v in (doc.get("pair_links") or {}).items()
                if isinstance(v, dict)
            }
        mesh.member_edges = {
            str(k): bool(v) for k, v in (doc.get("member_edges") or {}).items()
        }
        mesh.wire_requests = wire.load(doc.get("wire_requests"))
        leases_path = d / "leases.json"
        if leases_path.is_file():
            try:
                mesh.leases = mesh_ops.LeaseRegistry.from_dict(
                    json.loads(leases_path.read_text(encoding="utf-8"))
                )
            except (OSError, ValueError):
                mesh.leases = mesh_ops.LeaseRegistry()
        try:
            mesh.authority_epoch = int(doc.get("authority_epoch") or 0)
        except (TypeError, ValueError):
            mesh.authority_epoch = 0
        self._migrate_v2(mesh, doc)
        if "origin" in doc:
            mesh.origin = str(doc.get("origin") or "")
        elif mesh.primary:
            # Written before addresses: a mirror knew only its primary, which
            # is its creator unless authority has moved since — the best
            # name there is, and the one a peer addresses it by.
            mesh.origin = mesh.primary
            mesh._origin_migrated = True
        if mesh.origin and (self._is_me(mesh.origin) or mesh.origin == mesh.me):
            mesh.origin = ""
        mesh.invites = {
            str(k): str(v) for k, v in (doc.get("invites") or {}).items()
        }
        visibility = str(doc.get("visibility") or "private")
        mesh.visibility = visibility if visibility in VISIBILITIES else "private"
        mesh.offers = {
            str(k): dict(v) for k, v in (doc.get("offers") or {}).items()
            if isinstance(v, dict) and v.get("token")
        }
        notice = doc.get("rename_notice")
        if isinstance(notice, dict) and notice.get("old") and notice.get("pending"):
            mesh.rename_notice = {
                "old": str(notice["old"]),
                "pending": [str(p) for p in notice["pending"] if p],
            }
        mesh.pending_requests = {
            str(k): dict(v) for k, v in (doc.get("requests") or {}).items()
            if isinstance(v, dict)
        }
        mesh.pending_grants = {
            str(k): dict(v) for k, v in (doc.get("grants") or {}).items()
            if isinstance(v, dict)
        }
        mesh.policy = mesh_policy.load_policy(doc.get("policy"))
        mesh.roles_doc = mesh_roles.load_override(doc.get("roles"))
        try:
            mesh.roles_version = int(doc.get("roles_version") or 0)
        except (TypeError, ValueError):
            mesh.roles_version = 0
        log_path = d / "log.jsonl"
        if log_path.is_file():
            for line in log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    msg = json.loads(line)
                except ValueError:
                    continue
                mesh.messages.append(msg)
                if msg.get("id"):
                    mesh.seen_ids.add(str(msg["id"]))
                try:
                    mesh.next_seq = max(mesh.next_seq, int(msg.get("seq", -1)) + 1)
                except (TypeError, ValueError):
                    pass
        cursors_path = d / "cursors.json"
        if cursors_path.is_file():
            try:
                raw = json.loads(cursors_path.read_text(encoding="utf-8"))
                if {"members", "peers", "guests", "links"} & set(raw):
                    mesh.cursors = {
                        str(k): int(v) for k, v in (raw.get("members") or {}).items()
                    }
                    # "guests" is the phase-5 name for the same per-peer map.
                    mesh.link_cursors = {
                        str(k): int(v)
                        for k, v in (
                            raw.get("links") or raw.get("guests") or {}
                        ).items()
                    }
                    mesh.dismissed = {
                        str(k): {str(i) for i in v}
                        for k, v in (raw.get("dismissed") or {}).items()
                        if v
                    }
                    mesh.response_watches = {
                        str(k): dict(v)
                        for k, v in (raw.get("response_watches") or {}).items()
                        if isinstance(v, dict)
                    }
                    mesh.stranded_told = {
                        str(k): [str(sender) for sender in v]
                        for k, v in (raw.get("stranded_told") or {}).items()
                        if isinstance(v, list)
                    }
                else:  # phase-1 format: a flat {handle: index} map
                    mesh.cursors = {str(k): int(v) for k, v in raw.items()}
            except (ValueError, TypeError):
                mesh.cursors = {}
                mesh.link_cursors = {}
                mesh.dismissed = {}
                mesh.response_watches = {}
                mesh.stranded_told = {}
        outbox_path = d / "outbox.jsonl"
        if mesh.primary and outbox_path.is_file():
            for line in outbox_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    entry = json.loads(line)
                except ValueError:
                    continue
                if isinstance(entry, dict) and entry.get("id"):
                    mesh.outbox.append(entry)
        # Anything undelivered at load time counts as pending from now.
        now = time.monotonic()
        for handle, member in mesh.members.items():
            if self._is_local(mesh, member) and mesh.pending(handle):
                mesh._first_pending[handle] = now
                mesh.wake.set()
        if mesh.outbox:
            mesh.wake.set()
        return mesh

    def _persist_def(self, mesh: Mesh) -> None:
        d = self._mesh_dir(mesh.name)
        try:
            d.mkdir(parents=True, exist_ok=True)
            doc = {
                "name": mesh.wire_name,
                # Absent for a mesh created here, so its file is unchanged.
                **({"origin": mesh.origin} if mesh.origin else {}),
                "created_at": mesh.created_at,
                "self": mesh.me,
                # Absent for the default project, so a mesh that was never
                # filed writes exactly the file it always did.
                **({"project": mesh.project} if mesh.project else {}),
                "peers": mesh.peers,
                "links": mesh.links,
                "pair_links": mesh.pair_links,
                "authority_epoch": mesh.authority_epoch,
                # Absent while the member graph is complete, so a mesh that
                # never cuts a member edge writes exactly the file it always
                # did — and an older daemon reading it sees no new key.
                **({"member_edges": mesh.member_edges} if mesh.member_edges else {}),
                # Absent while nobody has been refused for want of an edge,
                # for the same reason as the line above it.
                **(
                    {"wire_requests": wire.dump(mesh.wire_requests)}
                    if mesh.wire_requests else {}
                ),
                "members": {h: m.to_dict() for h, m in sorted(mesh.members.items())},
                "invites": mesh.invites,
                # Both absent for a mesh nobody published, so an unpublished
                # mesh writes exactly the file it always did.
                **({"visibility": mesh.visibility}
                   if mesh.visibility != "private" else {}),
                **({"offers": mesh.offers} if mesh.offers else {}),
                **({"rename_notice": mesh.rename_notice}
                   if mesh.rename_notice else {}),
                "requests": mesh.pending_requests,
                "grants": mesh.pending_grants,
                "policy": mesh.policy,
                # Absent (not null) when the mesh runs the packaged
                # vocabulary, so mesh.json stays as small as it ever was for
                # the meshes that never touch this.
                **({"roles": mesh.roles_doc} if mesh.roles_doc else {}),
                **({"roles_version": mesh.roles_version}
                   if mesh.roles_version else {}),
            }
            path = d / "mesh.json"
            with atomic.scratch(path) as tmp:
                tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
                atomic.replace(tmp, path)
        except OSError as exc:
            log.warning("mesh %r: cannot persist definition: %s", mesh.name, exc)

    def _persist_cursors(self, mesh: Mesh) -> None:
        try:
            doc = {"members": mesh.cursors, "links": mesh.link_cursors}
            # Absent while nothing has been written off, so a mesh whose
            # operator never touches 'dismiss' writes exactly the file it
            # always did — and an older daemon reading it sees no new key.
            dismissed = {h: sorted(ids) for h, ids in mesh.dismissed.items() if ids}
            if dismissed:
                doc["dismissed"] = dismissed
            if mesh.response_watches:
                doc["response_watches"] = mesh.response_watches
            if mesh.stranded_told:
                doc["stranded_told"] = mesh.stranded_told
            path = self._mesh_dir(mesh.name) / "cursors.json"
            with atomic.scratch(path) as tmp:
                tmp.write_text(json.dumps(doc, indent=2), encoding="utf-8")
                atomic.replace(tmp, path)
        except OSError as exc:
            log.warning("mesh %r: cannot persist cursors: %s", mesh.name, exc)

    def _persist_leases(self, mesh: Mesh) -> None:
        d = self._mesh_dir(mesh.name)
        try:
            d.mkdir(parents=True, exist_ok=True)
            path = d / "leases.json"
            with atomic.scratch(path) as tmp:
                tmp.write_text(
                    json.dumps(mesh.leases.to_dict(), indent=2, ensure_ascii=False),
                    encoding="utf-8",
                )
                atomic.replace(tmp, path)
        except OSError as exc:
            log.warning("mesh %r: cannot persist leases: %s", mesh.name, exc)

    def _persist_outbox(self, mesh: Mesh) -> None:
        d = self._mesh_dir(mesh.name)
        try:
            d.mkdir(parents=True, exist_ok=True)
            path = d / "outbox.jsonl"
            with atomic.scratch(path) as tmp:
                tmp.write_text(
                    "".join(
                        json.dumps(e, ensure_ascii=False) + "\n"
                        for e in mesh.outbox
                    ),
                    encoding="utf-8",
                )
                atomic.replace(tmp, path)
        except OSError as exc:
            log.warning("mesh %r: cannot persist outbox: %s", mesh.name, exc)

    def _append_log(self, mesh: Mesh, msg: dict) -> None:
        d = self._mesh_dir(mesh.name)
        try:
            d.mkdir(parents=True, exist_ok=True)
            with open(d / "log.jsonl", "a", encoding="utf-8") as fh:
                fh.write(json.dumps(msg, ensure_ascii=False) + "\n")
        except OSError as exc:
            log.warning("mesh %r: cannot append log: %s", mesh.name, exc)

    @staticmethod
    def _response_watch_key(msg_id: str, recipient: str) -> str:
        return f"{msg_id}|{recipient}"

    def _watch_delivered_responses(
        self, mesh: Mesh, recipient: str, messages: List[dict]
    ) -> None:
        """Persist one response watch per delivered reply-expecting message."""
        changed = False
        delivered_at = utcnow()
        for msg in messages:
            msg_id = str(msg.get("id") or "")
            sender = str(msg.get("from") or "")
            if (
                not msg_id or sender not in mesh.members or sender == recipient
                or not expects_reply(msg_type_for(msg, recipient))
            ):
                continue
            key = self._response_watch_key(msg_id, recipient)
            if key not in mesh.response_watches:
                mesh.response_watches[key] = {
                    "id": msg_id, "from": sender, "to": recipient,
                    "delivered_at": delivered_at, "notices": 0,
                }
                changed = True
        if changed:
            self._persist_cursors(mesh)

    def _settle_response_watch(self, mesh: Mesh, reply: dict) -> None:
        """Clear what this member's send answers -- everything watched that
        was delivered to it before now.

        The same rule :meth:`Mesh.owed` settles on, deliberately: its walk
        stops at the member's own last send, so any message it sends closes
        every debt delivered to it beforehand. The watch used to clear only
        on a reply carrying ``reply_to``, and the two then disagreed. An ack
        sent without one closed the debt in the ledger and in the heartbeat
        and left the watch standing, so the sender was nudged at 5, 10, 15
        and 20 minutes about a message that had been answered -- four
        notices typed into that session's terminal, and a dismissal that
        was refused because the ledger had nothing left to dismiss
        (mesh-0826, 2026-09-21; claunch-response-nudge-disagrees-owed-ga9v8).

        Backwards only: a watch recorded after this send is a question the
        member has not seen yet, and :meth:`_record_response_watches` runs
        at delivery, which is after this.
        """
        sender = str(reply.get("from") or "")
        if not sender or sender not in mesh.members:
            return
        settled = [
            key for key, watch in mesh.response_watches.items()
            if str(watch.get("to") or "") == sender
        ]
        if settled:
            for key in settled:
                del mesh.response_watches[key]
            self._persist_cursors(mesh)

    def _response_watch_tick(self, mesh: Mesh) -> None:
        """Tell each sender once, and drop what can no longer be answered.

        A recipient with no terminal left is the closing this ledger had no
        way to make: a threaded reply cannot arrive from an exited session,
        so without this the watch outlives the session it names and keeps
        its line in the sender's rebrief for as long as the mesh exists. One
        mesh was carrying such a watch 18 days after the recipient exited.
        The sender is not told here -- :meth:`_report_stranded` already says
        who has gone, and saying it twice from two clocks is the drift this
        ledger was just brought out of.

        Guest members are left alone: :meth:`stranded_recipients` answers
        for local sessions only, because another daemon's liveness is not
        this one's to judge.
        """
        now = datetime.now(timezone.utc)
        changed = False
        waited_on = {
            str(w.get("to") or "") for w in mesh.response_watches.values()
        }
        waited_on.discard("")
        # ``exited`` only, not ``missing``. A session that exited is a
        # terminal that will not answer; a record that is gone entirely is a
        # roster question, and removing the member is what clears its watches
        # (see :meth:`leave`). Reading the second as an answer here
        # would let any failure to resolve a name erase the ledger quietly.
        gone = {
            str(e.get("handle") or "")
            for e in self.stranded_recipients(mesh, sorted(waited_on))
            if e.get("state") == "exited"
        }
        if gone:
            for key in [
                k for k, w in mesh.response_watches.items()
                if str(w.get("to") or "") in gone
            ]:
                del mesh.response_watches[key]
                changed = True
        for watch in list(mesh.response_watches.values()):
            try:
                notices = int(watch.get("notices") or 0)
            except (TypeError, ValueError):
                notices = 0
            if notices >= 1:
                continue
            age = _age_secs(watch.get("delivered_at"), now)
            if age is None or age < RESPONSE_NUDGE_AFTER:
                continue
            sender = str(watch.get("from") or "")
            recipient = str(watch.get("to") or "")
            msg_id = str(watch.get("id") or "")
            if not sender or sender not in mesh.members or not recipient or not msg_id:
                continue
            # The old id carried the notice's ordinal; with one notice there
            # is nothing to count, and a watch that reaches here has sent
            # none, so no earlier id can collide with this one.
            notice_id = f"response-nudge-{msg_id}-{recipient}-1"
            if notice_id not in mesh.seen_ids:
                body = (
                    f"no ack/reply from {recipient} for message {msg_id} after "
                    f"{RESPONSE_NUDGE_AFTER / 60:.0f} minutes. Decide whether "
                    "to request it again."
                )
                try:
                    self._send_core(mesh, mesh_policy.POLICY_SENDER, sender, body,
                                    external=True, type="fyi", msg_id=notice_id)
                except MeshError as exc:
                    log.debug("mesh %r: response nudge failed: %s", mesh.name, exc)
                    continue
            watch["notices"] = 1
            changed = True
        if changed:
            self._persist_cursors(mesh)


# --------------------------------------------------------------------------- #
# delivery formatting
# --------------------------------------------------------------------------- #
class _Literal(str):
    """Marker for strings the YAML dump should render as literal blocks."""


class _Dumper(yaml.SafeDumper):
    pass


_Dumper.add_representer(
    _Literal,
    lambda dumper, data: dumper.represent_scalar(
        "tag:yaml.org,2002:str", str(data), style="|"
    ),
)


def format_delivery(
    mesh_name: str,
    handle: str,
    msgs: List[dict],
    *,
    origins: Optional[Dict[str, str]] = None,
) -> str:
    """The fenced YAML block typed into the recipient's terminal.

    ``origins`` maps a sender's handle to the daemon it speaks from, with ""
    meaning the reader's own; a sender it does not mention gets no ``machine``
    line at all. Same-daemon is the common case and is written short, so what
    stands out in a batch is the sender the reader cannot reach by filesystem.

    An entry does NOT carry the message's own ``to``. In a 1:1 send it
    repeats the block's top-level ``to:`` verbatim, and in a multi-send it
    spells the whole recipient roster into every one of those recipients'
    terminals — a cost that grows with the fan-out and buys the reader
    nothing it acts on. The widest fan-out, ``to: "*"``, has never carried
    it, and the reader protocol in ``mesh_install`` has never listed it. The
    roster is still on the message in the log, so ``claunch mesh history``
    remains the way to ask who else received one (claunch-mesh-drop-batch-to-8lqg1).
    """
    batch = []
    for m in msgs:
        body = recipient_body(m, handle)
        if len(body) > MAX_DELIVERY_BODY:
            body = body[:MAX_DELIVERY_BODY] + " …[clipped — see mesh history]"
        entry: dict = {"id": m.get("id"), "from": m.get("from")}
        origin = (origins or {}).get(str(m.get("from") or ""))
        if origin is not None:
            entry["machine"] = "local" if not origin else f"{origin} (remote)"
        intent = str(msg_type_for(m, handle)).strip().lower() or "say"
        if intent != "say":
            entry["type"] = intent
        if m.get("reply_to"):
            entry["reply_to"] = m["reply_to"]
        entry["body"] = _Literal(body) if "\n" in body else body
        batch.append(entry)
    needs_reply = any(expects_reply(msg_type_for(m, handle)) for m in msgs)
    doc = {
        "mesh": mesh_name,
        "to": handle,
        "messages": len(batch),
        "needs_reply": needs_reply,
        "batch": batch,
        "note": (
            # The ack clause lives HERE, not only in the skill: this is the one
            # surface every member sees at the moment the duty applies, whatever
            # its role and whether or not it ever ran /mesh.
            "mesh messages delivered to your terminal — reply with: "
            f'claunch mesh send {mesh_name} <handle|*> "..."'
            " — if this puts work on you, answer NOW with a brief --type ack "
            "and --reply-to the message id, then send the outcome when it is done; "
            "silence reads as not received"
            if needs_reply
            else "fyi/ack only — no reply expected; drain and continue"
        ),
    }
    dumped = yaml.dump(
        doc, Dumper=_Dumper, sort_keys=False, allow_unicode=True, width=100
    )
    return (
        "---\n"
        "# claunch mesh: automated message delivery — machine-generated, "
        "not typed by the user\n" + dumped + "..."
    )
