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
import contextlib
import json
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, Iterable, List, Optional, Union

import yaml

from . import mesh_policy, mesh_roles, paths
from .manager import ManagerError, SessionManager
from .session import STATUS_IDLE

log = logging.getLogger("claunch.daemon.mesh")

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

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
REPLY_OPTIONAL_TYPES = frozenset({"fyi", "ack", "ping"})

#: The known message INTENTS. Other types are still accepted (and count as
#: reply-expected) but draw an advisory — see :func:`type_notice`.
INTENT_TYPES = frozenset({"say", "ask"}) | REPLY_OPTIONAL_TYPES


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
        f"type {message_type!r} is not a known intent (ask/say/fyi/ack) — it "
        "counts as reply-expected. 'type' is the message INTENT, not your "
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
        joined_at: str = "",
        wired: bool = False,
    ) -> None:
        self.handle = handle
        self.session = session
        self.machine = machine  # "" = this daemon; set by federation later
        self.role = role or infer_role(handle)
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

    def to_dict(self) -> dict:
        return {
            "handle": self.handle,
            "session": self.session,
            "machine": self.machine,
            "role": self.role,
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
            joined_at=str(doc.get("joined_at") or ""),
            wired=bool(doc.get("wired")),
        )


class Mesh:
    """One mesh: membership, its message log, and per-member delivery state."""

    def __init__(self, name: str, *, created_at: str = "", me: str = "") -> None:
        self.name = name
        self.created_at = created_at or utcnow()
        self.members: Dict[str, Member] = {}
        self.messages: List[dict] = []  # in-memory mirror of log.jsonl
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
        #: Outstanding invite tickets (authority only; pre-approval for a
        #: join request): token -> minted-at ISO timestamp. TTL-checked at
        #: redemption (MeshManager.invite_ttl).
        self.invites: Dict[str, str] = {}
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
        #: Nudge policy config (heartbeat / task-poll / stall warnings),
        #: persisted in mesh.json and edited via the API/web.
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

    def member_edge_table(self) -> List[dict]:
        """Every member pair with its state — what a topology view needs.

        Emitted for all pairs, not just the cut ones, because "connected" is
        a default rather than a stored fact: a caller handed only the cut set
        would have to know the default to draw the graph, and the two answers
        would drift the first time the default changed.
        """
        handles = sorted(self.members)
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
        out = [
            m for m in self.messages[start:]
            if self.addressed_to(m, handle) and m.get("id") not in done
        ]
        out.extend(
            m for m in self.provisional
            if self.addressed_to(m, handle) and m.get("id") not in done
        )
        return out

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
        """
        start = self.cursors.get(handle, 0)
        done = self.delivered_ids.get(handle) or frozenset()
        n = len(self.messages)
        out: List[dict] = []
        # Backwards from the newest, stopping at this member's own last send —
        # the log is walked by index rather than sliced because mesh_info calls
        # this for every member on every web poll.
        for k in range(n + len(self.provisional) - 1, -1, -1):
            if k < n:
                m = self.messages[k]
                delivered = k < start or m.get("id") in done
            else:
                m = self.provisional[k - n]
                delivered = m.get("id") in done
            if m.get("from") == handle:
                break
            if not delivered or not self.addressed_to(m, handle):
                continue
            if expects_reply(msg_type_for(m, handle)):
                out.append(m)
        out.reverse()
        return out


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
        self.relay_connected: Callable[[], bool] = lambda: False
        #: async () -> [machine names] — the other backends on our relay
        #: (RelayUplink.peer_list); None when no uplink or an old relay.
        self.peer_lister: Optional[Callable] = None
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

    @property
    def machine(self) -> str:
        return self._machine

    @machine.setter
    def machine(self, value: str) -> None:
        """Our relay name. The uplink resolves it *after* ``load_all``, so
        every mesh's ``me`` (and with it its rank) is refreshed here."""
        self._machine = str(value or "")
        for mesh in self._meshes.values():
            # An empty relay name never *erases* a remembered identity: a
            # federated mesh keeps the rank it was written with until the
            # uplink offers a real name (possibly a renamed one).
            if self._machine:
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
                            "joined_at": member.joined_at,
                            "members": len(mesh.members),
                        }
                    )
        return out

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def _mesh_root(self) -> Path:
        return self._root if self._root is not None else paths.mesh_root()

    def _mesh_dir(self, name: str) -> Path:
        return self._mesh_root() / name

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
        outgoing_path = root / "outgoing_joins.json"
        if outgoing_path.is_file():
            try:
                records = json.loads(outgoing_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                records = []
            for rec in records if isinstance(records, list) else []:
                if isinstance(rec, dict) and rec.get("request_id"):
                    self._outgoing[str(rec["request_id"])] = rec

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
    def create(self, name: str) -> Mesh:
        name = (name or "").strip()
        if not _NAME_RE.match(name):
            raise MeshError(
                f"invalid mesh name {name!r}: use letters, digits, '.', '_' or '-'"
            )
        if name in self._meshes:
            raise MeshConflict(f"mesh {name!r} already exists")
        mesh = Mesh(name, me=self.machine)
        self._meshes[name] = mesh
        self._persist_def(mesh)
        self._ensure_worker(name)
        return mesh

    def get(self, name: str) -> Mesh:
        try:
            return self._meshes[name]
        except KeyError:
            raise MeshError(f"no mesh named {name!r}") from None

    def list(self) -> List[Mesh]:
        return [self._meshes[k] for k in sorted(self._meshes)]

    def delete(self, name: str) -> None:
        mesh = self.get(name)
        # A primary tells its guests so their mirrors are dropped, not
        # orphaned (best-effort — an unreachable guest keeps a dead mirror
        # it can remove locally).
        if not mesh.primary and mesh.links:
            self._notify_unlink_soon(
                mesh.name,
                {m: str(g.get("token_out") or "") for m, g in mesh.links.items()},
            )
        self._drop_mesh(name)

    def _drop_mesh(self, name: str) -> None:
        mesh = self.get(name)
        task = self._workers.pop(name, None)
        if task is not None:
            task.cancel()
        del self._meshes[name]
        # Retire the directory rather than deleting history: rename with a
        # timestamp suffix so a recreated mesh starts clean.
        d = self._mesh_dir(mesh.name)
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
        code: Optional[str] = None,
    ):
        """Join a local session into a mesh — THE establishment verb.

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
        if mesh_name in self._meshes:
            mesh = self._meshes[mesh_name]
            if primary:
                ours = mesh.primary or self.machine
                if primary != ours:
                    raise MeshConflict(
                        f"mesh {mesh_name!r} on this daemon "
                        + (
                            f"is a mirror of {mesh.primary!r}"
                            if mesh.primary
                            else "is owned locally"
                        )
                        + f" — it cannot also join {mesh_name}@{primary}"
                    )
            return await self._join_local(
                mesh_name, session, handle=handle, role=role
            )
        if not primary:
            raise MeshError(
                f"no mesh named {mesh_name!r} on this daemon — join a remote "
                f"mesh with '{mesh_name}@<machine>', or create it first "
                f"(claunch mesh create {mesh_name})"
            )
        return await self._join_remote(
            mesh_name, primary, invite_token, session, handle=handle, role=role
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
        reply_token = secrets.token_urlsafe(18)
        body = {
            "mesh": mesh_name,
            "machine": machine,
            "session": session,
            "handle": handle,
            "role": role,
            "reply_token": reply_token,
        }
        if invite_token:
            body["code"] = invite_token
        resp = await self.peer_transport(primary, "/peer/mesh/join_request", body)
        if not isinstance(resp, dict):
            raise MeshError("unexpected response from the primary")
        if resp.get("granted"):
            return self._adopt_grant(
                mesh_name, primary, reply_token, resp.get("grant") or {}
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
        self, mesh_name: str, primary: str, reply_token: str, grant: dict
    ) -> Member:
        """Build the mirror + our member from a primary's grant payload."""
        if mesh_name in self._meshes:
            raise MeshConflict(
                f"a mesh named {mesh_name!r} appeared locally while the join "
                "was pending — remove it and re-join"
            )
        mesh = Mesh(mesh_name, me=self.machine)
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
        member_doc = grant.get("member") or {}
        member = mesh.members.get(str(member_doc.get("handle") or ""))
        if member is None:
            raise MeshError("grant is missing our member record")
        try:
            cursor = int(grant.get("cursor"))
        except (TypeError, ValueError):
            cursor = len(mesh.messages)
        mesh.cursors[member.handle] = cursor
        self._meshes[mesh_name] = mesh
        self._persist_def(mesh)
        self._persist_cursors(mesh)
        self._ensure_worker(mesh_name)
        self._brief_soon(mesh, member)
        log.info(
            "mesh %r: joined as %r (mirror of %r)",
            mesh_name, member.handle, primary,
        )
        return member

    async def _join_local(
        self,
        name: str,
        session: str,
        *,
        handle: str = "",
        role: str = "",
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
        member = Member(handle, session, role=self._resolve_role(mesh, handle, role))
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

    def _stance_lines(self, mesh: Mesh, member: Member) -> str:
        """The briefing's stance section for this member — possibly empty.

        A POINTER, not the prose. Pasting the stance inline was tried and is
        wrong twice over: it doubles the length of a block that is typed into
        a live terminal, and — the part that actually matters — it freezes the
        stance into the agent's context at join time, so every later upload
        would leave the member acting on a vocabulary the mesh no longer has.
        ``mesh stance`` always prints the current text, and doubles as the
        recovery path after a compaction drops this block.
        """
        role = mesh.roleset.get(member.role)
        if not (role and role.stance.strip()):
            return ""
        return (
            f"stance: run 'claunch mesh stance {mesh.name}' now — it prints "
            f"what a {member.role} is on this mesh, and it is binding\n"
        )

    def briefing_block(self, mesh: Mesh, member: Member) -> str:
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
        reachable = mesh.neighbours(member.handle)
        others = ", ".join(
            f"{h} ({mesh.members[h].role})" for h in reachable
        ) or "(nobody else yet)"
        hidden = len(mesh.members) - 1 - len(reachable)
        return (
            "---\n"
            "# claunch mesh: join briefing -- machine-generated, not typed by the user\n"
            f"mesh: {mesh.name}\n"
            f"you: {member.handle} (role: {member.role})\n"
            f"members: {others}\n"
            + (
                f"note: {hidden} other member(s) exist that you are not "
                "connected to and cannot message\n" if hidden > 0 else ""
            ) +
            f"send: claunch mesh send {mesh.name} <to|*> \"...\"\n"
            + self._stance_lines(mesh, member) +
            f"protocol: activate your 'mesh' skill NOW (/mesh {mesh.name}) to "
            "load the member protocol; if you have no such skill, run "
            "'claunch install' first and retry\n"
            f"note: incoming mesh messages will be typed into this terminal\n"
            "---"
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
            if session.status() == STATUS_IDLE:
                break
            await asyncio.sleep(0.5)
        else:
            return  # never went idle; skip rather than interleave
        # best-effort: a dead session just misses it
        await session.deliver(self.briefing_block(mesh, member))

    async def leave(self, name: str, handle: str) -> Member:
        """Remove a member. Guests may only remove their OWN members (the
        request is forwarded); the primary may remove anyone (kick)."""
        mesh = self.get(name)
        member = mesh.members.get(handle)
        if member is None:
            raise MeshError(f"no member {handle!r} in mesh {name!r}")
        if mesh.primary:
            if member.machine != self.machine:
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
            self._persist_cursors(mesh)
            self._roster_changed(mesh)
        return member

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
    ) -> dict:
        """Send a message into the mesh.

        ``sender`` is a member handle or a local session name; ``external``
        admits a non-member sender (the human on the dashboard). ``to`` is
        ``"*"``, a handle, or a list of handles. ``type`` is the message
        *intent* (``say``/``ask`` invite a reply; ``fyi``/``ack`` do not).
        ``reply_to`` threads this message to an earlier message id.

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
        if mesh.primary:
            return await self._send_from_mirror(
                mesh, sender, to, body, external=external, type=type,
                reply_to=reply_to, sections=sections,
            )
        result = self._send_core(
            mesh, sender, to, body, external=external, type=type,
            reply_to=reply_to, sections=sections,
        )
        self._flush_guests_soon(mesh)
        return result

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
        if norm_sections is not None:
            msg["shared"] = body
            msg["sections"] = norm_sections
        mesh.next_seq += 1
        self._fold_provisional(mesh, msg["id"])
        mesh.messages.append(msg)
        mesh.seen_ids.add(msg["id"])
        mesh.last_append = time.monotonic()
        self._append_log(mesh, msg)
        now = time.monotonic()
        if member is not None and self._is_local(mesh, member):
            mesh.activity.setdefault(member.handle, {"anchor": now})[
                "last_sent"
            ] = now
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
        note = type_notice(intent)
        if not note and norm_sections:
            for sec in norm_sections.values():
                if sec.get("type"):
                    note = type_notice(sec["type"])
                    if note:
                        break
        if note:
            advisories.append(note)
        return {
            **msg,
            "recipients": recipients,
            "queued": False,
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
        return {
            **entry,
            "queued": True,
            "recipients": [],
            "remote": [],
            "queued_remote": [],
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
            raise MeshError(
                f"{from_handle!r} has no connection to {', '.join(sorted(cut))} "
                f"in mesh {mesh.name!r} — it can reach: {reachable}. Ask the "
                "session that spawned you to connect you, or route through a "
                "peer you share."
            )
        return targets

    def history(self, name: str, limit: int = 50) -> List[dict]:
        mesh = self.get(name)
        return mesh.messages[-limit:] if limit > 0 else list(mesh.messages)

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
        """
        try:
            return mesh.roleset.resolve(handle, role)
        except mesh_roles.RoleError as exc:
            raise MeshError(str(exc)) from None

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
                    "task_poll": r.task_poll,
                    "stance": r.stance,
                    # How many members currently hold it — the roster is the
                    # only place the vocabulary meets reality, and a role
                    # nobody holds is worth seeing as such.
                    "members": sorted(
                        h for h, m in mesh.members.items() if m.role == r.name
                    ),
                }
                for r in (rs.roles[n] for n in sorted(rs.roles))
            ],
            # Roles held by a member but no longer in the vocabulary — the
            # visible face of "uploads are not retroactive".
            "orphans": sorted(
                {m.role for m in mesh.members.values() if not rs.get(m.role)}
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
        mesh = self.get(name)
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
        # Absolute roster before anything moves: see _stamp_own_members.
        self._stamp_own_members(mesh)
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
        mesh = self.get(name)
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
            self._persist_def(mesh)
            return {"a": a, "b": b, "enabled": bool(enabled)}
        self._mark_member_edge(mesh, a, b, enabled)
        self._persist_def(mesh)
        self._roster_changed(mesh)
        self._flush_guests_soon(mesh)
        log.info(
            "mesh %r: %s the member edge %s <-> %s",
            mesh.name, "connected" if enabled else "disconnected", a, b,
        )
        return {"a": a, "b": b, "enabled": bool(enabled)}

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
                role=member.role, tier=tier, root=root
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
        mesh = self.get(name)
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
                "mesh": mesh.name,
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
                {"v": 2, "mesh": mesh.name, "machine": machine, "token": token}
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
            "mesh": mesh.name,
            "machine": me,
            "session": session,
            "handle": handle,
            "role": role,
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
        parent: str = "",
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
        member = Member(
            handle, session, machine=machine,
            role=self._resolve_role(mesh, handle, role),
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
            mesh.peers.insert(0, mesh.me)
            self._stamp_own_members(mesh)
        if machine and machine not in mesh.peers:
            mesh.peers.append(machine)

    @staticmethod
    def _stamp_own_members(mesh: Mesh) -> None:
        """Give our own members an explicit machine.

        Before phase 7 a blank ``machine`` meant "the authority's own", which
        was unambiguous only because authority never moved. It moves now, so
        the roster has to be absolute: a member left blank would be claimed
        by whoever holds rank 0 next, and delivery would follow it to a
        daemon that does not have the session.
        """
        if not mesh.me:
            return
        for member in mesh.members.values():
            if not member.machine:
                member.machine = mesh.me

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

    def _grant_payload(self, mesh: Mesh, machine: str, member: Member) -> dict:
        """Everything a peer needs to build its mirror + member.

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
            "member": member.to_dict(),
            "cursor": len(mesh.messages),
            "peers": list(mesh.peers),
            "epoch": mesh.authority_epoch,
            "links": self._link_grants_for(mesh, machine),
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
    ) -> dict:
        """A remote daemon asks to enrol one of its sessions.

        Three outcomes: an already-trusted machine is auto-granted (mirror
        recovery); a valid invite ticket grants synchronously; anything else
        pends for operator approval. The self-declared ``machine`` cannot be
        verified here — but the grant travels via the relay to the *claimed*
        name, so only the daemon really registered under it can finish.
        """
        mesh = self.get(name)
        self._require_authority(mesh, "membership")
        if not _NAME_RE.match(machine or ""):
            raise MeshError("invalid peer machine name")
        if not str(reply_token or ""):
            raise MeshError("missing reply token")
        if machine == self._require_machine():
            raise MeshError("a daemon cannot join itself as a guest")
        if machine in mesh.links or code:
            if code:
                self._redeem_invite(mesh, code)
            member, created = self._admit_member(
                mesh, machine, session, handle, role
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
            "reply_token": str(reply_token),
            "requested_at": utcnow(),
        }
        self._persist_def(mesh)
        log.info(
            "mesh %r: join request %s from %r (%r) awaits approval",
            mesh.name, rid, machine, handle,
        )
        return {"pending": True, "id": rid}

    def request_list(self, name: str) -> List[dict]:
        """Pending inbound join requests, oldest first (primary only)."""
        mesh = self.get(name)
        self._require_authority(mesh, "membership")
        return [
            {
                k: r.get(k)
                for k in ("id", "machine", "session", "handle", "role",
                          "requested_at")
            }
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
        member, created = self._admit_member(
            mesh, req["machine"], req["session"], req["handle"], req["role"]
        )
        self._register_guest(mesh, req["machine"], req["reply_token"])
        if created:
            mesh.roster_version += 1
        mesh.pending_grants[rid] = {
            "machine": req["machine"],
            "handle": member.handle,
            "reply_token": req["reply_token"],
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
                        "mesh": mesh.name,
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
            member = mesh.members.get(g["handle"])
            if member is None or machine not in mesh.links:
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
                        "mesh": mesh.name,
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
                    mesh.name, machine, g["handle"], exc,
                )
                mesh.pending_grants.pop(rid, None)
                self._rollback_admission(mesh, machine, g["handle"])
                continue
            mesh.pending_grants.pop(rid, None)
            status.update({"ok": True, "error": None, "retry_at": 0.0,
                           "backoff": 0.0})
            self._persist_def(mesh)
            log.info(
                "mesh %r: grant delivered to %r (%r)",
                mesh.name, machine, g["handle"],
            )

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

    def _rollback_admission(self, mesh: Mesh, machine: str, handle: str) -> None:
        mesh.members.pop(handle, None)
        mesh.remote_activity.pop(handle, None)
        mesh.remote_lineage.pop(handle, None)
        if not any(m.machine == machine for m in mesh.members.values()):
            self._unrank(mesh, machine)
        mesh.roster_version += 1
        self._persist_def(mesh)
        self._persist_cursors(mesh)
        self._flush_guests_soon(mesh)

    async def revoke_guest(self, name: str, machine: str) -> dict:
        """Unlink a guest machine: drop its members, credentials and mirror."""
        mesh = self.get(name)
        self._require_authority(mesh, "guest management")
        guest = mesh.links.get(machine)
        if guest is None:
            raise MeshError(f"no guest {machine!r} linked to mesh {name!r}")
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
        mesh.roster_version += 1
        self._persist_def(mesh)
        self._persist_cursors(mesh)
        self._flush_guests_soon(mesh)
        if self.peer_transport is not None:
            try:
                await self.peer_transport(
                    machine,
                    "/peer/mesh/unlink",
                    {
                        "mesh": mesh.name,
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
        )
        return {"ok": True, "handle": member.handle}

    async def peer_invite_accept(
        self,
        name: str,
        machine: str,
        session: str,
        handle: str,
        role: str,
        code: str,
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
        mesh = self.get(name)
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
                          "role", "requested_at")
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
            (root / "outgoing_joins.json").write_text(
                json.dumps(list(self._outgoing.values()), indent=2),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("cannot persist outgoing join requests: %s", exc)

    def peer_join_accept(
        self, name: str, machine: str, token: str,
        session: str, handle: str, role: str, parent: str = "",
    ) -> dict:
        """A guest daemon asks to enrol one of its sessions as a member."""
        mesh = self.get(name)
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
        member = Member(
            handle, session, machine=machine,
            role=self._resolve_role(mesh, handle, role),
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
        mesh = self.get(name)
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
        self._roster_changed(mesh)
        return member.to_dict()

    def peer_send_accept(
        self, name: str, machine: str, token: str, message: dict
    ) -> dict:
        """A guest daemon forwards a member's send for sequencing."""
        mesh = self.get(name)
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
        result = self._send_core(
            mesh,
            sender,
            to,
            str(message.get("body") or ""),
            external=external,
            type=str(message.get("type") or "say"),
            reply_to=str(message.get("reply_to") or "") or None,
            sections=sections if isinstance(sections, dict) else None,
            msg_id=mid,
            sender_machine=None if external else machine,
            ts=str(message.get("ts") or "") or None,
        )
        self._flush_guests_soon(mesh)
        return result

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
        mesh = self.get(name)
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
        mesh = self.get(name)
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
        """Inject a primary-decided policy nudge into a local member's PTY.

        Idleness is re-checked at fire time (the primary decided from a
        report that may be stale); a busy member simply drops the nudge —
        the primary's timers re-fire it later.
        """
        handle = str(nudge.get("handle") or "")
        member = mesh.members.get(handle)
        if member is None or not self._is_local(mesh, member):
            return
        try:
            session = self.manager.get(member.session)
        except ManagerError:
            return
        if session.exited or session.status() != STATUS_IDLE:
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
            unanswered = last_asked > 0 and last_sent < last_asked
            pending = len(mesh.pending(handle))
            anchor = st.get("anchor", now)
            active_at = max(last_sent, st.get("last_delivered", 0.0), anchor)
            first_pending = mesh._first_pending.get(handle)
            report[handle] = {
                "idle": session.status() == STATUS_IDLE,
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
                    "mesh": mesh.name,
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

    def owed_report(self, mesh: Mesh) -> dict:
        """Per-member ledger of unanswered mail: who owes what, since when.

        Backs ``claunch mesh owed`` and the web dashboard. Local members are
        read straight off the log (:meth:`Mesh.owed`); remote ones only
        through the activity their own daemon piggybacks on the sync ack,
        which carries counts rather than messages — so their rows are marked
        ``reported`` and the per-message detail lives on that daemon. This is
        the same split ``pending`` has always made, for the same reason: a
        guest owns its members' cursors.
        """
        now = datetime.now(timezone.utc)
        rows = []
        for handle in sorted(mesh.members):
            member = mesh.members[handle]
            local = self._is_local(mesh, member)
            row: dict = {
                "handle": handle,
                "role": member.role,
                "machine": member.machine or mesh.me or "",
                "session": member.session,
                "local": local,
                "reachability": self._reachability(mesh, member),
                "source": "log" if local else "reported",
                "messages": [],
                "owed": None,
                "pending": None,
                "oldest_age": None,
                "stale": False,
            }
            if local:
                owed = mesh.owed(handle)
                row["pending"] = len(mesh.pending(handle))
                row["owed"] = len(owed)
                for m in owed:
                    body = recipient_body(m, handle)
                    row["messages"].append(
                        {
                            "id": m.get("id"),
                            "from": m.get("from"),
                            "type": msg_type_for(m, handle),
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
                rep = mesh.remote_activity.get(handle)
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

    def mesh_info(self, mesh: Mesh) -> dict:
        members = []
        lineage = self._local_lineage(mesh)
        for handle in sorted(mesh.members):
            m = mesh.members[handle]
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
                    "pending": len(mesh.pending(handle)) if local else None,
                    "owed": owed,
                    "reachability": self._reachability(mesh, m),
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
                        # A blank machine only survives on a mesh that never
                        # federated, where it can only mean us.
                        if (m.machine or mesh.me) == machine
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
                    }
                )
        return {
            "name": mesh.name,
            "created_at": mesh.created_at,
            "primary": mesh.primary or None,
            "authority": mesh.authority or None,
            "self": mesh.me or None,
            "epoch": mesh.authority_epoch,
            "members": members,
            "messages": len(mesh.messages),
            "provisional": len(mesh.provisional),
            "peers": peers,
            "links": self.edge_table(mesh),
            # The member graph, one layer up from `links`: who may message
            # whom. Every pair is listed with its state — see
            # Mesh.member_edge_table on why the cut set alone is not enough.
            "member_links": mesh.member_edge_table(),
            "requests": requests,
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
                    await mesh_policy.tick(self, mesh)
                except Exception:  # noqa: BLE001 — a policy bug must not stall delivery
                    log.exception("mesh %r: policy tick failed", mesh.name)
        except asyncio.CancelledError:
            pass

    async def _deliver_to(self, mesh: Mesh, member: Member) -> None:
        pending = mesh.pending(member.handle)
        if not pending:
            mesh._first_pending.pop(member.handle, None)
            return
        try:
            session = self.manager.get(member.session)
        except ManagerError:
            return  # session removed; hold the cursor, deliver on rejoin/respawn
        if session.exited:
            return  # hold until respawn (same name, same cursor)
        if session.status() != STATUS_IDLE:
            held = time.monotonic() - mesh._first_pending.get(
                member.handle, time.monotonic()
            )
            if held < self.busy_hold:
                return  # idle-gate: don't interleave with a running turn
        block = format_delivery(mesh.name, member.handle, pending)
        if not await session.deliver(block):
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
        self._stamp_own_members(mesh)
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
        try:
            mesh.authority_epoch = int(doc.get("authority_epoch") or 0)
        except (TypeError, ValueError):
            mesh.authority_epoch = 0
        self._migrate_v2(mesh, doc)
        mesh.invites = {
            str(k): str(v) for k, v in (doc.get("invites") or {}).items()
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
                else:  # phase-1 format: a flat {handle: index} map
                    mesh.cursors = {str(k): int(v) for k, v in raw.items()}
            except (ValueError, TypeError):
                mesh.cursors = {}
                mesh.link_cursors = {}
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
                "name": mesh.name,
                "created_at": mesh.created_at,
                "self": mesh.me,
                "peers": mesh.peers,
                "links": mesh.links,
                "pair_links": mesh.pair_links,
                "authority_epoch": mesh.authority_epoch,
                # Absent while the member graph is complete, so a mesh that
                # never cuts a member edge writes exactly the file it always
                # did — and an older daemon reading it sees no new key.
                **({"member_edges": mesh.member_edges} if mesh.member_edges else {}),
                "members": {h: m.to_dict() for h, m in sorted(mesh.members.items())},
                "invites": mesh.invites,
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
            (d / "mesh.json").write_text(
                json.dumps(doc, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("mesh %r: cannot persist definition: %s", mesh.name, exc)

    def _persist_cursors(self, mesh: Mesh) -> None:
        try:
            (self._mesh_dir(mesh.name) / "cursors.json").write_text(
                json.dumps(
                    {"members": mesh.cursors, "links": mesh.link_cursors},
                    indent=2,
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("mesh %r: cannot persist cursors: %s", mesh.name, exc)

    def _persist_outbox(self, mesh: Mesh) -> None:
        d = self._mesh_dir(mesh.name)
        try:
            d.mkdir(parents=True, exist_ok=True)
            (d / "outbox.jsonl").write_text(
                "".join(
                    json.dumps(e, ensure_ascii=False) + "\n" for e in mesh.outbox
                ),
                encoding="utf-8",
            )
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


def format_delivery(mesh_name: str, handle: str, msgs: List[dict]) -> str:
    """The fenced YAML block typed into the recipient's terminal."""
    batch = []
    for m in msgs:
        body = recipient_body(m, handle)
        if len(body) > MAX_DELIVERY_BODY:
            body = body[:MAX_DELIVERY_BODY] + " …[clipped — see mesh history]"
        entry: dict = {"id": m.get("id"), "from": m.get("from")}
        if m.get("to") != "*":
            entry["to"] = m.get("to")
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
            "and send the outcome when it is done; silence reads as not received"
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
