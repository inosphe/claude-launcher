"""Mesh: session-to-session messaging delivered by typing into PTYs.

Sessions are grouped into a *mesh*; each member gets a handle, and messages
sent between members are **injected into the recipient's terminal** by the
daemon (bracketed paste + Enter). Arrival is the wake-up — receivers need no
watcher, no polling and no hooks, which is why the whole doorbell/nudge
apparatus of file-based agent meshes is absent here by design (see
``docs/mesh-design.md``).

Ownership model: this daemon owns its local members — their memberships, its
copy of the message log, and delivery into its own PTYs. Remote members
(``machine`` set) are placeholders until the federation phase routes to them
over the relay; their messages stay queued in the log meanwhile.

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
        #: Federation links, peer machine -> {token_in, token_out, created_at}.
        #: token_in authenticates the peer's calls to us; token_out is what we
        #: present when calling them (exchanged by the link handshake).
        self.links: Dict[str, dict] = {}
        #: Outstanding invite tokens (created here, consumed by a peer's link).
        self.invites: Dict[str, str] = {}
        #: Per-peer forward cursor into ``messages`` (what they have received).
        self.peer_cursors: Dict[str, int] = {}
        #: Runtime peer flush status: machine -> {ok, error, retry_at, backoff}.
        self.peer_status: Dict[str, dict] = {}
        self.seen_ids: set = set()  # message-id dedupe for peer deliveries
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
        #: async (machine, path, body) -> dict; raises on failure.
        self.peer_transport: Optional[Callable] = None
        self.relay_connected: Callable[[], bool] = lambda: False

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

    def join(
        self,
        name: str,
        session: str,
        *,
        handle: str = "",
        role: str = "",
    ) -> Member:
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
            if m.local and m.session == session:
                raise MeshConflict(
                    f"session {session!r} is already in mesh {name!r} as {m.handle!r}"
                )
        member = Member(handle, session, role=role)
        mesh.members[handle] = member
        # New members start caught up: joining must not replay the backlog.
        mesh.cursors[handle] = len(mesh.messages)
        self._persist_def(mesh)
        self._persist_cursors(mesh)
        self._push_members_soon(mesh)
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
            f"note: incoming mesh messages will be typed into this terminal\n"
            "---"
        )
        try:
            await session.paste(block, enter=True)
        except Exception:  # noqa: BLE001 — best-effort (SessionGone etc.)
            return

    def leave(self, name: str, handle: str) -> Member:
        mesh = self.get(name)
        member = mesh.members.pop(handle, None)
        if member is None:
            raise MeshError(f"no member {handle!r} in mesh {name!r}")
        mesh.cursors.pop(handle, None)
        mesh._first_pending.pop(handle, None)
        self._persist_def(mesh)
        self._persist_cursors(mesh)
        self._push_members_soon(mesh)
        return member

    def resolve_sender(self, name: str, sender: str) -> Optional[Member]:
        """Resolve a handle *or* a local session name to a member."""
        mesh = self.get(name)
        if sender in mesh.members:
            return mesh.members[sender]
        for m in mesh.members.values():
            if m.local and m.session == sender:
                return m
        return None

    # ------------------------------------------------------------------ #
    # messaging
    # ------------------------------------------------------------------ #
    def send(
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
        """Append a message to the mesh log and wake the delivery worker.

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
        """
        mesh = self.get(name)
        member = self.resolve_sender(name, sender)
        if member is None and not external:
            raise MeshError(
                f"{sender!r} is neither a handle nor a member session in mesh "
                f"{name!r} (join first: claunch mesh join {name})"
            )
        from_handle = member.handle if member is not None else sender
        body = _CTRL_RE.sub("", str(body)).strip()
        recipients = self._resolve_recipients(mesh, from_handle, to)
        if not recipients:
            raise MeshError(f"mesh {name!r} has no other members to deliver to")
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
            "id": "msg-" + uuid.uuid4().hex[:12],
            "ts": utcnow(),
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
            # Origin machine: only the originating daemon forwards a message
            # to its peers, so a broadcast never echoes back and forth.
            "origin": self.machine,
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
        if member is not None and member.local:
            mesh.activity.setdefault(member.handle, {"anchor": now})[
                "last_sent"
            ] = now
        for handle in recipients:
            mesh._first_pending.setdefault(handle, now)
        mesh.wake.set()
        remote = [h for h in recipients if h in mesh.members and not mesh.members[h].local]
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
            # Remote recipients ride the peer flusher; when the relay is down
            # they are queued (durably, via the peer cursor) until reconnect.
            "queued_remote": remote if not self.relay_connected() else [],
            "remote": remote,
            "batched": norm_sections is not None,
            "expects_reply": expects_reply(intent),
            # Advisory, not an error: an unknown intent invites reply-all,
            # and a body @-addressing several recipients wants to be a batch.
            "notice": " ".join(advisories) if advisories else None,
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
        """Apply a partial policy edit (validated deep-merge) and persist."""
        mesh = self.get(name)
        try:
            mesh.policy = mesh_policy.merge_policy(mesh.policy, patch)
        except mesh_policy.PolicyError as exc:
            raise MeshError(f"bad policy: {exc}") from None
        self._persist_def(mesh)
        return mesh.policy

    # ------------------------------------------------------------------ #
    # federation (relay-optional: every daemon owns its local side; links
    # are agreements negotiated over the relay while it is up)
    # ------------------------------------------------------------------ #
    def _require_machine(self) -> str:
        if not self.machine:
            raise MeshError(
                "cross-machine mesh needs a relay identity — configure the "
                "relay uplink first (claunch daemon relay url/name/token)"
            )
        return self.machine

    def invite(self, name: str) -> dict:
        """Mint an invite code a peer daemon redeems with ``mesh link``.

        The invite token doubles as the peer's credential for calling us
        (token_in) once the link handshake completes — the handshake's
        reply_token becomes our credential for calling them.
        """
        mesh = self.get(name)
        machine = self._require_machine()
        token = secrets.token_urlsafe(18)
        mesh.invites[token] = utcnow()
        self._persist_def(mesh)
        code = base64.urlsafe_b64encode(
            json.dumps(
                {"v": 1, "mesh": mesh.name, "machine": machine, "token": token}
            ).encode("utf-8")
        ).decode("ascii")
        return {"code": code, "mesh": mesh.name, "machine": machine}

    async def link(self, code: str) -> dict:
        """Redeem an invite code: handshake with the origin daemon over the
        relay, exchanging mesh-scoped tokens and member lists."""
        machine = self._require_machine()
        if self.peer_transport is None:
            raise MeshError("relay uplink is not running — cannot reach peers")
        try:
            doc = json.loads(base64.urlsafe_b64decode(code.strip().encode("ascii")))
            peer = str(doc["machine"])
            mesh_name = str(doc["mesh"])
            token = str(doc["token"])
        except Exception:
            raise MeshError("invalid invite code") from None
        if peer == machine:
            raise MeshError("this invite was minted by this very daemon")
        if mesh_name in self._meshes:
            mesh = self._meshes[mesh_name]
        else:
            mesh = self.create(mesh_name)
        reply_token = secrets.token_urlsafe(18)
        payload = await self.peer_transport(
            peer,
            "/peer/mesh/link",
            {
                "mesh": mesh_name,
                "machine": machine,
                "token": token,
                "reply_token": reply_token,
                "members": [m.to_dict() for m in mesh.members.values() if m.local],
            },
        )
        mesh.links[peer] = {
            "token_out": token,  # we authenticate to them with the invite token
            "token_in": reply_token,  # they authenticate to us with our reply
            "created_at": utcnow(),
        }
        mesh.peer_cursors.setdefault(peer, len(mesh.messages))
        self._merge_remote_members(mesh, peer, payload.get("members") or [])
        self._persist_cursors(mesh)
        return {
            "mesh": mesh_name,
            "peer": peer,
            "members": len(mesh.members),
        }

    def _check_peer_token(self, mesh: Mesh, machine: str, token: str) -> None:
        link = mesh.links.get(machine)
        expected = link.get("token_in") if link else None
        if expected is None or not secrets.compare_digest(
            str(token).encode("utf-8"), str(expected).encode("utf-8")
        ):
            raise MeshError("bad mesh peer token")

    def peer_link_accept(
        self,
        name: str,
        machine: str,
        token: str,
        reply_token: str,
        members: List[dict],
    ) -> dict:
        """Handle a peer daemon redeeming one of our invites."""
        mesh = self.get(name)
        if not _NAME_RE.match(machine or ""):
            raise MeshError("invalid peer machine name")
        matched = next(
            (t for t in mesh.invites if secrets.compare_digest(
                t.encode("utf-8"), str(token).encode("utf-8"))),
            None,
        )
        if matched is None:
            raise MeshError("unknown or already-used invite token")
        del mesh.invites[matched]
        mesh.links[machine] = {
            "token_in": matched,  # they call us with the invite token
            "token_out": str(reply_token),  # we call them with their reply
            "created_at": utcnow(),
        }
        mesh.peer_cursors.setdefault(machine, len(mesh.messages))
        self._merge_remote_members(mesh, machine, members)
        self._persist_cursors(mesh)
        log.info("mesh %r: linked with peer %r", mesh.name, machine)
        return {
            "machine": self._require_machine(),
            "members": [m.to_dict() for m in mesh.members.values() if m.local],
        }

    def peer_messages_accept(
        self, name: str, machine: str, token: str, messages: List[dict]
    ) -> dict:
        """Ingest a batch of messages forwarded by a linked peer daemon."""
        mesh = self.get(name)
        self._check_peer_token(mesh, machine, token)
        accepted = 0
        now = time.monotonic()
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
                "origin": machine,
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
            accepted += 1
            for handle, member in mesh.members.items():
                if member.local and mesh.addressed_to(msg, handle):
                    mesh._first_pending.setdefault(handle, now)
        if accepted:
            mesh.last_append = time.monotonic()
            mesh.wake.set()
        return {"ok": True, "accepted": accepted}

    def peer_members_accept(
        self, name: str, machine: str, token: str, members: List[dict]
    ) -> dict:
        """Replace our view of a linked peer's local members."""
        mesh = self.get(name)
        self._check_peer_token(mesh, machine, token)
        self._merge_remote_members(mesh, machine, members)
        return {"ok": True}

    def _merge_remote_members(
        self, mesh: Mesh, machine: str, members: List[dict]
    ) -> None:
        """Adopt the peer's *local* members as our remote entries for it."""
        for handle in [
            h for h, m in mesh.members.items() if m.machine == machine
        ]:
            del mesh.members[handle]
            mesh.cursors.pop(handle, None)
        for doc in members:
            if not isinstance(doc, dict) or doc.get("machine"):
                continue  # only the peer's own local members; no transitivity
            handle = str(doc.get("handle") or "")
            if not _NAME_RE.match(handle):
                continue
            if handle in mesh.members:
                log.warning(
                    "mesh %r: handle %r from peer %r clashes with an existing "
                    "member — skipped", mesh.name, handle, machine,
                )
                continue
            mesh.members[handle] = Member(
                handle,
                str(doc.get("session") or ""),
                machine=machine,
                role=str(doc.get("role") or ""),
                joined_at=str(doc.get("joined_at") or ""),
            )
        self._persist_def(mesh)

    def _push_members_soon(self, mesh: Mesh) -> None:
        """Best-effort fanout of our member list to linked peers (on change)."""
        if not mesh.links or self.peer_transport is None:
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        asyncio.ensure_future(self._push_members(mesh))

    async def _push_members(self, mesh: Mesh) -> None:
        payload = [m.to_dict() for m in mesh.members.values() if m.local]
        for machine, link in list(mesh.links.items()):
            try:
                await self.peer_transport(
                    machine,
                    "/peer/mesh/members",
                    {
                        "mesh": mesh.name,
                        "machine": self.machine,
                        "token": link["token_out"],
                        "members": payload,
                    },
                )
            except Exception as exc:  # noqa: BLE001 — next flush retries implicitly
                log.debug("mesh %r: member push to %r failed: %s",
                          mesh.name, machine, exc)

    def pending_for_machine(self, mesh: Mesh, machine: str) -> List[dict]:
        """Locally-originated messages a linked peer has not received yet."""
        start = mesh.peer_cursors.get(machine, 0)
        out = []
        for m in mesh.messages[start:]:
            if not self.machine or m.get("origin", "") != self.machine:
                continue
            if self._machine_addressed(mesh, m, machine):
                out.append(m)
        return out

    def _machine_addressed(self, mesh: Mesh, msg: dict, machine: str) -> bool:
        to = msg.get("to")
        sender = msg.get("from")
        for handle, member in mesh.members.items():
            if member.machine != machine or handle == sender:
                continue
            if to == "*" or to == handle or (isinstance(to, list) and handle in to):
                return True
        return False

    async def _flush_peer(self, mesh: Mesh, machine: str) -> None:
        status = mesh.peer_status.setdefault(
            machine, {"ok": None, "error": None, "retry_at": 0.0, "backoff": 0.0}
        )
        now = time.monotonic()
        if now < status["retry_at"]:
            return
        pending = self.pending_for_machine(mesh, machine)
        target = len(mesh.messages)
        if not pending:
            mesh.peer_cursors[machine] = target
            return
        link = mesh.links.get(machine)
        if link is None or self.peer_transport is None:
            return
        try:
            await self.peer_transport(
                machine,
                "/peer/mesh/messages",
                {
                    "mesh": mesh.name,
                    "machine": self.machine,
                    "token": link["token_out"],
                    "messages": pending,
                },
            )
        except Exception as exc:  # noqa: BLE001 — queue and retry with backoff
            backoff = min(
                (status["backoff"] or _PEER_BACKOFF_BASE / 2) * 2, _PEER_BACKOFF_MAX
            )
            mesh.peer_status[machine] = {
                "ok": False,
                "error": str(exc),
                "retry_at": now + backoff,
                "backoff": backoff,
            }
            log.info(
                "mesh %r: %d message(s) queued for peer %r (%s); retry in %.0fs",
                mesh.name, len(pending), machine, exc, backoff,
            )
            return
        mesh.peer_cursors[machine] = target
        mesh.peer_status[machine] = {
            "ok": True, "error": None, "retry_at": 0.0, "backoff": 0.0,
        }
        self._persist_cursors(mesh)
        log.info(
            "mesh %r: forwarded %d message(s) to peer %r",
            mesh.name, len(pending), machine,
        )

    # ------------------------------------------------------------------ #
    # views
    # ------------------------------------------------------------------ #
    def mesh_info(self, mesh: Mesh) -> dict:
        members = []
        for handle in sorted(mesh.members):
            m = mesh.members[handle]
            members.append(
                {
                    **m.to_dict(),
                    "pending": len(mesh.pending(handle)) if m.local else None,
                    "reachability": self._reachability(m),
                }
            )
        peers = []
        for machine in sorted(mesh.links):
            status = mesh.peer_status.get(machine) or {}
            peers.append(
                {
                    "machine": machine,
                    "linked_at": mesh.links[machine].get("created_at", ""),
                    "ok": status.get("ok"),
                    "error": status.get("error"),
                    "queued": len(self.pending_for_machine(mesh, machine)),
                }
            )
        return {
            "name": mesh.name,
            "created_at": mesh.created_at,
            "members": members,
            "messages": len(mesh.messages),
            "peers": peers,
            "policy": mesh.policy,
        }

    def _reachability(self, member: Member) -> str:
        if not member.local:
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
                    if member is None or not member.local:
                        continue
                    try:
                        await self._deliver_to(mesh, member)
                    except Exception:  # noqa: BLE001 — one member must not stall the mesh
                        log.exception(
                            "mesh %r: delivery to %r failed", mesh.name, handle
                        )
                if self.peer_transport is not None:
                    for machine in list(mesh.links):
                        try:
                            await self._flush_peer(mesh, machine)
                        except Exception:  # noqa: BLE001
                            log.exception(
                                "mesh %r: peer flush to %r failed",
                                mesh.name, machine,
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
        mesh.links = {
            str(k): dict(v) for k, v in (doc.get("links") or {}).items()
            if isinstance(v, dict)
        }
        mesh.invites = {
            str(k): str(v) for k, v in (doc.get("invites") or {}).items()
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
                if "members" in raw or "peers" in raw:
                    mesh.cursors = {
                        str(k): int(v) for k, v in (raw.get("members") or {}).items()
                    }
                    mesh.peer_cursors = {
                        str(k): int(v) for k, v in (raw.get("peers") or {}).items()
                    }
                else:  # phase-1 format: a flat {handle: index} map
                    mesh.cursors = {str(k): int(v) for k, v in raw.items()}
            except (ValueError, TypeError):
                mesh.cursors = {}
                mesh.peer_cursors = {}
        # Anything undelivered at load time counts as pending from now.
        now = time.monotonic()
        for handle, member in mesh.members.items():
            if member.local and mesh.pending(handle):
                mesh._first_pending[handle] = now
                mesh.wake.set()
        return mesh

    def _persist_def(self, mesh: Mesh) -> None:
        d = self._mesh_dir(mesh.name)
        try:
            d.mkdir(parents=True, exist_ok=True)
            doc = {
                "name": mesh.name,
                "created_at": mesh.created_at,
                "members": {h: m.to_dict() for h, m in sorted(mesh.members.items())},
                "links": mesh.links,
                "invites": mesh.invites,
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
                    {"members": mesh.cursors, "peers": mesh.peer_cursors}, indent=2
                ),
                encoding="utf-8",
            )
        except OSError as exc:
            log.warning("mesh %r: cannot persist cursors: %s", mesh.name, exc)

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
