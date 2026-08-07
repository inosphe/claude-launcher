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
import json
import logging
import re
import time
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Union

import yaml

from . import paths
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

#: Wait for a message burst to go quiet before delivering (seconds).
DEFAULT_SETTLE = 2.0

#: How long to hold delivery for a busy session before injecting anyway —
#: harnesses like claude queue text typed during a turn, so this is safe; the
#: hold just avoids interleaving with short turns. Seconds.
DEFAULT_BUSY_HOLD = 60.0

#: Worker rescan cadence while messages are pending (seconds).
_POLL = 1.0


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
    ) -> None:
        self.manager = manager
        self.settle = settle
        self.busy_hold = busy_hold
        self._meshes: Dict[str, Mesh] = {}
        self._workers: Dict[str, asyncio.Task] = {}
        self._started = False

    # ------------------------------------------------------------------ #
    # lifecycle
    # ------------------------------------------------------------------ #
    def load_all(self) -> None:
        root = paths.mesh_root()
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
        d = paths.mesh_dir(mesh.name)
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
        return member

    def leave(self, name: str, handle: str) -> Member:
        mesh = self.get(name)
        member = mesh.members.pop(handle, None)
        if member is None:
            raise MeshError(f"no member {handle!r} in mesh {name!r}")
        mesh.cursors.pop(handle, None)
        mesh._first_pending.pop(handle, None)
        self._persist_def(mesh)
        self._persist_cursors(mesh)
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
    ) -> dict:
        """Append a message to the mesh log and wake the delivery worker.

        ``sender`` is a member handle or a local session name; ``external``
        admits a non-member sender (the human on the dashboard). ``to`` is
        ``"*"``, a handle, or a list of handles.
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
        if not body:
            raise MeshError("empty message body")
        recipients = self._resolve_recipients(mesh, from_handle, to)
        if not recipients:
            raise MeshError(f"mesh {name!r} has no other members to deliver to")
        msg = {
            "id": "msg-" + uuid.uuid4().hex[:12],
            "ts": utcnow(),
            "from": from_handle,
            "to": to if isinstance(to, str) else list(to),
            "body": body,
        }
        mesh.messages.append(msg)
        mesh.last_append = time.monotonic()
        self._append_log(mesh, msg)
        now = time.monotonic()
        for handle in recipients:
            mesh._first_pending.setdefault(handle, now)
        mesh.wake.set()
        return {
            **msg,
            "recipients": recipients,
            "queued_remote": [
                h for h in recipients if not mesh.members[h].local
            ],
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
        return {
            "name": mesh.name,
            "created_at": mesh.created_at,
            "members": members,
            "messages": len(mesh.messages),
        }

    def _reachability(self, member: Member) -> str:
        if not member.local:
            return "remote"  # federation refines this to connected/disconnected
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
        log_path = d / "log.jsonl"
        if log_path.is_file():
            for line in log_path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    mesh.messages.append(json.loads(line))
                except ValueError:
                    continue
        cursors_path = d / "cursors.json"
        if cursors_path.is_file():
            try:
                raw = json.loads(cursors_path.read_text(encoding="utf-8"))
                mesh.cursors = {str(k): int(v) for k, v in raw.items()}
            except (ValueError, TypeError):
                mesh.cursors = {}
        # Anything undelivered at load time counts as pending from now.
        now = time.monotonic()
        for handle, member in mesh.members.items():
            if member.local and mesh.pending(handle):
                mesh._first_pending[handle] = now
                mesh.wake.set()
        return mesh

    def _persist_def(self, mesh: Mesh) -> None:
        d = paths.mesh_dir(mesh.name)
        try:
            d.mkdir(parents=True, exist_ok=True)
            doc = {
                "name": mesh.name,
                "created_at": mesh.created_at,
                "members": {h: m.to_dict() for h, m in sorted(mesh.members.items())},
            }
            (d / "mesh.json").write_text(
                json.dumps(doc, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("mesh %r: cannot persist definition: %s", mesh.name, exc)

    def _persist_cursors(self, mesh: Mesh) -> None:
        try:
            (paths.mesh_dir(mesh.name) / "cursors.json").write_text(
                json.dumps(mesh.cursors, indent=2), encoding="utf-8"
            )
        except OSError as exc:
            log.warning("mesh %r: cannot persist cursors: %s", mesh.name, exc)

    def _append_log(self, mesh: Mesh, msg: dict) -> None:
        d = paths.mesh_dir(mesh.name)
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
        body = str(m.get("body") or "")
        if len(body) > MAX_DELIVERY_BODY:
            body = body[:MAX_DELIVERY_BODY] + " …[clipped — see mesh history]"
        entry: dict = {"from": m.get("from")}
        if m.get("to") != "*":
            entry["to"] = m.get("to")
        entry["body"] = _Literal(body) if "\n" in body else body
        batch.append(entry)
    doc = {
        "mesh": mesh_name,
        "to": handle,
        "messages": len(batch),
        "batch": batch,
        "note": (
            "mesh messages delivered to your terminal — reply with: "
            f'claunch mesh send {mesh_name} <handle|*> "..."'
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
