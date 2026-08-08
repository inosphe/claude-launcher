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
import json
import logging
import re
import secrets
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable, Dict, List, Optional, Union

import yaml

from . import mesh_policy, paths
from .manager import ManagerError, SessionManager
from .session import STATUS_IDLE, SessionGone

log = logging.getLogger("claunch.daemon.mesh")

_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Handle leading word -> role, interconnect's convention (`worker_1` is a
#: worker, `moderator` leads). Anything else is a plain member.
_ROLE_WORDS = {
    "leader": "leader",
    "moderator": "leader",
    "operator": "operator",
    "worker": "worker",
    "reviewer": "reviewer",
    "specialist": "specialist",
}

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
    head = re.split(r"[._\-0-9]", handle.lower(), maxsplit=1)[0]
    return _ROLE_WORDS.get(head, "member")


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
    ) -> None:
        self.handle = handle
        self.session = session
        self.machine = machine  # "" = this daemon; set by federation later
        self.role = role or infer_role(handle)
        self.joined_at = joined_at or utcnow()

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
        }

    @classmethod
    def from_dict(cls, doc: dict) -> "Member":
        return cls(
            str(doc["handle"]),
            str(doc.get("session") or ""),
            machine=str(doc.get("machine") or ""),
            role=str(doc.get("role") or ""),
            joined_at=str(doc.get("joined_at") or ""),
        )


class Mesh:
    """One mesh: membership, its message log, and per-member delivery state."""

    def __init__(self, name: str, *, created_at: str = "") -> None:
        self.name = name
        self.created_at = created_at or utcnow()
        self.members: Dict[str, Member] = {}
        self.messages: List[dict] = []  # in-memory mirror of log.jsonl
        self.cursors: Dict[str, int] = {}  # handle -> delivered log index
        #: Role: "" = this daemon is the mesh's PRIMARY (owner); a machine
        #: name = this mesh is a MIRROR of that primary daemon.
        self.primary: str = ""
        #: Mirror side: the credential pair for talking to the primary
        #: ({token_in, token_out, created_at}); None on the primary.
        self.link: Optional[dict] = None
        #: Primary side: guest machine -> {token_in, token_out, created_at}.
        #: token_in authenticates that guest's calls to us; token_out is what
        #: we present when syncing to it (exchanged by the link handshake).
        self.guests: Dict[str, dict] = {}
        #: Outstanding invite tickets (primary only; pre-approval for a
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
        #: Primary side: per-guest fanout cursor into ``messages``.
        self.guest_cursors: Dict[str, int] = {}
        #: Runtime peer call status: machine -> {ok, error, retry_at, backoff,
        #: last_sync, roster_seen}. On a mirror the single key is the primary.
        self.peer_status: Dict[str, dict] = {}
        #: Mirror side: durable upstream queue of sends the primary has not
        #: accepted yet (persisted in outbox.jsonl; drains strictly in order).
        self.outbox: List[dict] = []
        #: Primary side: freshest activity report per remote member handle
        #: (piggybacked on guest sync acks; read by the policy tick).
        self.remote_activity: Dict[str, dict] = {}
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
        #: In-memory per-member activity/timers the policy tick reads:
        #: handle -> {anchor, last_sent, last_delivered, hb_/tp_/warn_ timers}.
        self.activity: Dict[str, dict] = {}
        self.wake = asyncio.Event()
        self.last_append = 0.0  # monotonic time of the last log append
        self._first_pending: Dict[str, float] = {}  # handle -> monotonic

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    def addressed_to(self, msg: dict, handle: str) -> bool:
        to = msg.get("to")
        if to == "*":
            return msg.get("from") != handle
        if isinstance(to, list):
            return handle in to
        return to == handle

    def pending(self, handle: str) -> List[dict]:
        start = self.cursors.get(handle, 0)
        return [m for m in self.messages[start:] if self.addressed_to(m, handle)]


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
        self._started = False
        #: Federation wiring, set by the daemon entrypoint once the uplink
        #: exists. ``machine`` is this daemon's relay name — the machine-level
        #: qualifier in global addresses like ``work-pc/s0``; empty means no
        #: relay configured, so the mesh is local-only.
        self.machine: str = ""
        #: async (machine, path, body) -> dict; raises PeerUnreachable on
        #: transport failure, MeshError on an application-level rejection.
        self.peer_transport: Optional[Callable] = None
        self.relay_connected: Callable[[], bool] = lambda: False
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

    def _is_local(self, mesh: Mesh, member: Member) -> bool:
        """Whether ``member``'s session lives on THIS daemon.

        The roster is absolute (v2): the primary's own members carry
        ``machine == ""``, guest members carry their guest's machine name. On
        a mirror, only members stamped with our machine are ours.
        """
        if mesh.primary:
            return bool(self.machine) and member.machine == self.machine
        return member.machine in ("", self.machine)

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
            try:
                self._meshes[entry.name] = self._load(entry)
            except (OSError, ValueError, KeyError) as exc:
                log.warning("skipping unreadable mesh %r: %s", entry.name, exc)
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
        mesh = Mesh(name)
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
        if not mesh.primary and mesh.guests:
            self._notify_unlink_soon(
                mesh.name,
                {m: str(g.get("token_out") or "") for m, g in mesh.guests.items()},
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
        mesh = Mesh(mesh_name)
        mesh.primary = primary
        mesh.link = {
            "token_out": str(grant.get("token") or ""),
            "token_in": reply_token,
            "created_at": utcnow(),
        }
        for entry in grant.get("members") or []:
            if isinstance(entry, dict) and entry.get("handle"):
                member = Member.from_dict(entry)
                mesh.members[member.handle] = member
        for m in grant.get("messages") or []:
            if not isinstance(m, dict) or not m.get("id"):
                continue
            mesh.messages.append(m)
            mesh.seen_ids.add(str(m["id"]))
            self._append_log(mesh, m)
        mesh.policy = mesh_policy.load_policy(grant.get("policy"))
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
                {"session": session, "handle": handle, "role": role},
            )
            member = Member.from_dict(payload)
            mesh.members[member.handle] = member
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
        member = Member(handle, session, role=role)
        mesh.members[handle] = member
        # New members start caught up: joining must not replay the backlog.
        mesh.cursors[handle] = len(mesh.messages)
        self._persist_cursors(mesh)
        self._roster_changed(mesh)
        self._brief_soon(mesh, member)
        return member

    def _brief_soon(self, mesh: Mesh, member: Member) -> None:
        """Schedule a join briefing injection into the new member's terminal."""
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.ensure_future(self._brief(mesh, member))

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
        others = ", ".join(
            f"{h} ({m.role})" for h, m in sorted(mesh.members.items())
            if h != member.handle
        ) or "(nobody else yet)"
        block = (
            "---\n"
            "# claunch mesh: join briefing -- machine-generated, not typed by the user\n"
            f"mesh: {mesh.name}\n"
            f"you: {member.handle} (role: {member.role})\n"
            f"members: {others}\n"
            f"send: claunch mesh send {mesh.name} <to|*> \"...\"\n"
            f"protocol: activate your 'mesh' skill NOW (/mesh {mesh.name}) to "
            "load the member protocol; if you have no such skill, run "
            "'claunch mesh install' first and retry\n"
            f"note: incoming mesh messages will be typed into this terminal\n"
            "---"
        )
        try:
            await session.paste(block, enter=True)
        except Exception:  # noqa: BLE001 — best-effort (SessionGone etc.)
            return

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
        if mesh.primary:
            self._persist_def(mesh)
            self._persist_cursors(mesh)
        else:
            mesh.remote_activity.pop(handle, None)
            self._persist_cursors(mesh)
            self._roster_changed(mesh)
        return member

    def _roster_changed(self, mesh: Mesh) -> None:
        """Primary-side roster bump: persist and fan out to guests soon."""
        mesh.roster_version += 1
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
            raise MeshError(
                f"mesh {mesh.name!r} has no other members to deliver to"
            )
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
            raise MeshError(
                f"mesh {mesh.name!r} has no other members to deliver to"
            )
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
        return {
            **entry,
            "queued": True,
            "recipients": [],
            "remote": [],
            "queued_remote": [],
            "batched": norm_sections is not None,
            "expects_reply": expects_reply(intent),
            "notice": (
                f"queued: primary daemon {mesh.primary!r} is unreachable — "
                "the message will forward on reconnect"
            ),
        }

    def _resolve_recipients(
        self, mesh: Mesh, from_handle: str, to: Union[str, List[str]]
    ) -> List[str]:
        if to == "*":
            return [h for h in mesh.members if h != from_handle]
        targets = [to] if isinstance(to, str) else list(to)
        unknown = [t for t in targets if t not in mesh.members]
        if unknown:
            raise MeshError(
                f"unknown recipient(s) in mesh {mesh.name!r}: {', '.join(unknown)}"
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
    # federation v2: primary/mirror. The primary owns roster, log, policy
    # and invites; guests hold a synced mirror and forward member requests.
    # ------------------------------------------------------------------ #
    def _require_machine(self) -> str:
        if not self.machine:
            raise MeshError(
                "cross-machine mesh needs a relay identity — configure the "
                "relay uplink first (claunch daemon relay url/name/token)"
            )
        return self.machine

    def _require_primary(self, mesh: Mesh, what: str) -> None:
        if mesh.primary:
            raise MeshError(
                f"mesh {mesh.name!r} is a mirror — {what} is owned by the "
                f"primary daemon ({mesh.primary})"
            )

    async def _peer_call_primary(self, mesh: Mesh, path: str, body: dict) -> dict:
        """One authenticated request from this mirror to its primary."""
        if self.peer_transport is None:
            raise PeerUnreachable(
                "relay uplink is not running — cannot reach the primary"
            )
        link = mesh.link or {}
        return await self.peer_transport(
            mesh.primary,
            path,
            {
                "mesh": mesh.name,
                "machine": self._require_machine(),
                "token": str(link.get("token_out") or ""),
                **body,
            },
        )

    def invite(self, name: str) -> dict:
        """Mint a pre-approval invite ticket (primary-only).

        A ticket lets ``mesh join name@machine --code X`` skip the pending
        queue — the unattended/automation path. Tickets are single-use and
        expire after ``invite_ttl`` seconds.
        """
        mesh = self.get(name)
        self._require_primary(mesh, "invite minting")
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
        self._require_primary(mesh, "invite minting")
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
        self._require_primary(mesh, "invite minting")
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

    def _check_guest_token(self, mesh: Mesh, machine: str, token: str) -> None:
        guest = mesh.guests.get(machine)
        expected = guest.get("token_in") if guest else None
        if expected is None or not secrets.compare_digest(
            str(token).encode("utf-8"), str(expected).encode("utf-8")
        ):
            raise MeshError("bad mesh peer token")

    def _check_primary_token(self, mesh: Mesh, machine: str, token: str) -> None:
        expected = (mesh.link or {}).get("token_in") if mesh.primary else None
        if (
            expected is None
            or machine != mesh.primary
            or not secrets.compare_digest(
                str(token).encode("utf-8"), str(expected).encode("utf-8")
            )
        ):
            raise MeshError("bad mesh peer token")

    # -- primary-side: join requests, grants, guest lifecycle ------------ #
    def _admit_member(
        self, mesh: Mesh, machine: str, session: str, handle: str, role: str
    ):
        """Admit (or reclaim) a guest member — returns (member, created).

        The same (machine, session) re-joining reclaims its existing member
        record instead of conflicting: that is the mirror-lost recovery
        path, not a duplicate.
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
        member = Member(handle, session, machine=machine, role=role)
        mesh.members[handle] = member
        return member, True

    def _register_guest(self, mesh: Mesh, machine: str, reply_token: str) -> None:
        """Mint (or re-mint) the credential pair for a guest machine."""
        mesh.guests[machine] = {
            "token_in": secrets.token_urlsafe(18),  # guest -> us
            "token_out": str(reply_token),  # us -> guest (their choice)
            "created_at": utcnow(),
        }
        mesh.guest_cursors[machine] = len(mesh.messages)
        mesh.peer_status[machine] = {
            "ok": True, "error": None, "retry_at": 0.0, "backoff": 0.0,
            "last_sync": time.monotonic(), "roster_seen": mesh.roster_version,
        }

    def _grant_payload(self, mesh: Mesh, machine: str, member: Member) -> dict:
        """Everything a guest needs to build its mirror + member."""
        mesh.guest_cursors[machine] = len(mesh.messages)
        status = mesh.peer_status.get(machine)
        if status is not None:
            status["roster_seen"] = mesh.roster_version
        return {
            "token": mesh.guests[machine]["token_in"],
            "members": [m.to_dict() for m in mesh.members.values()],
            "messages": list(mesh.messages),
            "policy": mesh.policy,
            "member": member.to_dict(),
            "cursor": len(mesh.messages),
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
        self._require_primary(mesh, "membership")
        if not _NAME_RE.match(machine or ""):
            raise MeshError("invalid peer machine name")
        if not str(reply_token or ""):
            raise MeshError("missing reply token")
        if machine == self._require_machine():
            raise MeshError("a daemon cannot join itself as a guest")
        if machine in mesh.guests or code:
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
        self._require_primary(mesh, "membership")
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
        self._require_primary(mesh, "membership")
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
        self._require_primary(mesh, "membership")
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
            if member is None or machine not in mesh.guests:
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

    def _rollback_admission(self, mesh: Mesh, machine: str, handle: str) -> None:
        mesh.members.pop(handle, None)
        mesh.remote_activity.pop(handle, None)
        if not any(m.machine == machine for m in mesh.members.values()):
            mesh.guests.pop(machine, None)
            mesh.guest_cursors.pop(machine, None)
            mesh.peer_status.pop(machine, None)
            mesh.pending_nudges.pop(machine, None)
        mesh.roster_version += 1
        self._persist_def(mesh)
        self._persist_cursors(mesh)
        self._flush_guests_soon(mesh)

    async def revoke_guest(self, name: str, machine: str) -> dict:
        """Unlink a guest machine: drop its members, credentials and mirror."""
        mesh = self.get(name)
        self._require_primary(mesh, "guest management")
        guest = mesh.guests.pop(machine, None)
        if guest is None:
            raise MeshError(f"no guest {machine!r} linked to mesh {name!r}")
        mesh.guest_cursors.pop(machine, None)
        mesh.peer_status.pop(machine, None)
        mesh.pending_nudges.pop(machine, None)
        for rid in [
            r for r, g in mesh.pending_grants.items() if g["machine"] == machine
        ]:
            mesh.pending_grants.pop(rid, None)
        removed = [h for h, m in mesh.members.items() if m.machine == machine]
        for h in removed:
            mesh.members.pop(h, None)
            mesh.remote_activity.pop(h, None)
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
        session: str, handle: str, role: str,
    ) -> dict:
        """A guest daemon asks to enrol one of its sessions as a member."""
        mesh = self.get(name)
        self._require_primary(mesh, "membership")
        self._check_guest_token(mesh, machine, token)
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
        member = Member(handle, session, machine=machine, role=role)
        mesh.members[handle] = member
        self._roster_changed(mesh)
        log.info("mesh %r: %r joined from guest %r", mesh.name, handle, machine)
        return {**member.to_dict(), "cursor": len(mesh.messages)}

    def peer_leave_accept(
        self, name: str, machine: str, token: str, handle: str
    ) -> dict:
        """A guest daemon withdraws one of its OWN members."""
        mesh = self.get(name)
        self._require_primary(mesh, "membership")
        self._check_guest_token(mesh, machine, token)
        member = mesh.members.get(handle)
        if member is None:
            raise MeshError(f"no member {handle!r} in mesh {name!r}")
        if member.machine != machine:
            raise MeshError(
                f"{handle!r} does not belong to daemon {machine!r}"
            )
        mesh.members.pop(handle, None)
        mesh.remote_activity.pop(handle, None)
        self._roster_changed(mesh)
        return member.to_dict()

    def peer_send_accept(
        self, name: str, machine: str, token: str, message: dict
    ) -> dict:
        """A guest daemon forwards a member's send for sequencing."""
        mesh = self.get(name)
        self._require_primary(mesh, "sequencing")
        self._check_guest_token(mesh, machine, token)
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

    # -- guest-side peer handler ---------------------------------------- #
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
    ) -> dict:
        """The primary pushes state at us: log tail, roster, policy, nudges.

        ``base`` must equal our log length — a mismatch means the primary's
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
            mid = str(m.get("id") or "")
            if not mid or mid in mesh.seen_ids or not isinstance(m.get("body"), str):
                continue
            msg = {
                "id": mid,
                "ts": str(m.get("ts") or utcnow()),
                "from": str(m.get("from") or machine),
                "to": m.get("to") if isinstance(m.get("to"), (str, list)) else "*",
                "type": str(m.get("type") or "say").strip().lower() or "say",
                "body": _CTRL_RE.sub("", m["body"]),
            }
            if m.get("reply_to"):
                msg["reply_to"] = str(m["reply_to"])
            # Batch fields ride along so this daemon can slice deliveries
            # for its own local members.
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
            mesh.messages.append(msg)
            mesh.seen_ids.add(mid)
            self._append_log(mesh, msg)
            appended += 1
            for handle, member in mesh.members.items():
                if self._is_local(mesh, member) and mesh.addressed_to(msg, handle):
                    mesh._first_pending.setdefault(handle, now)
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
        try:
            await session.paste(block, enter=True)
        except Exception:  # noqa: BLE001 — SessionGone etc.
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
        if mesh.primary or not mesh.guests or self.peer_transport is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return

        async def _flush_all() -> None:
            for machine in list(mesh.guests):
                try:
                    await self._flush_guest(mesh, machine)
                except Exception:  # noqa: BLE001
                    log.exception(
                        "mesh %r: guest flush to %r failed", mesh.name, machine
                    )

        asyncio.ensure_future(_flush_all())

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

    async def _flush_guest(self, mesh: Mesh, machine: str) -> None:
        """Primary → guest sync: log tail + roster + policy + nudges.

        Also fires with an empty payload on a slow cadence while any policy
        is enabled, so the guest's activity report (piggybacked on the ack)
        stays fresh for the policy engine.
        """
        if mesh.primary:
            return
        guest = mesh.guests.get(machine)
        if guest is None or self.peer_transport is None:
            return
        status = mesh.peer_status.setdefault(
            machine, {"ok": None, "error": None, "retry_at": 0.0, "backoff": 0.0,
                      "last_sync": 0.0, "roster_seen": 0}
        )
        now = time.monotonic()
        if now < status.get("retry_at", 0.0):
            return
        cursor = mesh.guest_cursors.get(machine, 0)
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
        if not msgs and not nudges and not roster_due and not report_due:
            return
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
            return
        if not isinstance(resp, dict):
            resp = {}
        if "resync" in resp:
            # Our cursor was stale (state loss on either side): adopt the
            # guest's true position; the next flush sends the real tail.
            try:
                mesh.guest_cursors[machine] = max(0, int(resp["resync"]))
            except (TypeError, ValueError):
                pass
            status.update({"ok": True, "error": None, "retry_at": 0.0,
                           "backoff": 0.0, "last_sync": now})
            self._persist_cursors(mesh)
            return
        mesh.guest_cursors[machine] = cursor + len(msgs)
        status.update({"ok": True, "error": None, "retry_at": 0.0,
                       "backoff": 0.0, "last_sync": now,
                       "roster_seen": mesh.roster_version})
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
        self._persist_cursors(mesh)
        if msgs:
            log.info(
                "mesh %r: synced %d message(s) to guest %r",
                mesh.name, len(msgs), machine,
            )

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
    def mesh_info(self, mesh: Mesh) -> dict:
        members = []
        for handle in sorted(mesh.members):
            m = mesh.members[handle]
            local = self._is_local(mesh, m)
            members.append(
                {
                    **m.to_dict(),
                    "pending": len(mesh.pending(handle)) if local else None,
                    "reachability": self._reachability(mesh, m),
                }
            )
        peers = []
        if mesh.primary:
            status = mesh.peer_status.get(mesh.primary) or {}
            peers.append(
                {
                    "machine": mesh.primary,
                    "role": "primary",
                    "linked_at": (mesh.link or {}).get("created_at", ""),
                    "ok": status.get("ok"),
                    "error": status.get("error"),
                    "queued": len(mesh.outbox),
                }
            )
        else:
            for machine in sorted(mesh.guests):
                status = mesh.peer_status.get(machine) or {}
                peers.append(
                    {
                        "machine": machine,
                        "role": "guest",
                        "linked_at": mesh.guests[machine].get("created_at", ""),
                        "ok": status.get("ok"),
                        "error": status.get("error"),
                        "queued": (
                            len(mesh.messages)
                            - mesh.guest_cursors.get(machine, 0)
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
            "members": members,
            "messages": len(mesh.messages),
            "peers": peers,
            "requests": requests,
            "policy": mesh.policy,
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
                    # Mirror duties: drain the upstream outbox. The policy
                    # engine deliberately does NOT run here — it lives on
                    # the primary.
                    if self.peer_transport is not None:
                        try:
                            await self._flush_upstream(mesh)
                        except Exception:  # noqa: BLE001
                            log.exception(
                                "mesh %r: upstream flush failed", mesh.name
                            )
                    continue
                if self.peer_transport is not None:
                    for machine in list(mesh.guests):
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
        try:
            await session.paste(block, enter=True)
        except SessionGone:
            return
        mesh.cursors[member.handle] = len(mesh.messages)
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
    def _load(self, d: Path) -> Mesh:
        doc = json.loads((d / "mesh.json").read_text(encoding="utf-8"))
        mesh = Mesh(str(doc["name"]), created_at=str(doc.get("created_at") or ""))
        for entry in (doc.get("members") or {}).values():
            member = Member.from_dict(entry)
            mesh.members[member.handle] = member
        # v1 symmetric-federation state ("links", cursors "peers") is
        # deliberately dropped, not migrated — the model was retired.
        mesh.primary = str(doc.get("primary") or "")
        link = doc.get("link")
        mesh.link = dict(link) if isinstance(link, dict) else None
        if mesh.primary and mesh.link is None:
            log.warning(
                "mesh %r: mirror of %r has no link credentials — unlinked",
                mesh.name, mesh.primary,
            )
        mesh.guests = {
            str(k): dict(v) for k, v in (doc.get("guests") or {}).items()
            if isinstance(v, dict)
        }
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
        cursors_path = d / "cursors.json"
        if cursors_path.is_file():
            try:
                raw = json.loads(cursors_path.read_text(encoding="utf-8"))
                if "members" in raw or "peers" in raw or "guests" in raw:
                    mesh.cursors = {
                        str(k): int(v) for k, v in (raw.get("members") or {}).items()
                    }
                    mesh.guest_cursors = {
                        str(k): int(v) for k, v in (raw.get("guests") or {}).items()
                    }
                else:  # phase-1 format: a flat {handle: index} map
                    mesh.cursors = {str(k): int(v) for k, v in raw.items()}
            except (ValueError, TypeError):
                mesh.cursors = {}
                mesh.guest_cursors = {}
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
                "primary": mesh.primary,
                "link": mesh.link,
                "guests": mesh.guests,
                "members": {h: m.to_dict() for h, m in sorted(mesh.members.items())},
                "invites": mesh.invites,
                "requests": mesh.pending_requests,
                "grants": mesh.pending_grants,
                "policy": mesh.policy,
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
                    {"members": mesh.cursors, "guests": mesh.guest_cursors},
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
        if m.get("sections") is not None:
            # batch message: this recipient reads only the shared preamble
            # plus its own slice — never another member's instructions
            sec = (m.get("sections") or {}).get(handle)
            body = _slice_body(
                str(m.get("shared") or ""),
                sec.get("text") if isinstance(sec, dict) else None,
            )
        else:
            body = str(m.get("body") or "")
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
            "mesh messages delivered to your terminal — reply with: "
            f'claunch mesh send {mesh_name} <handle|*> "..."'
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
