"""Who can answer a delegated decision — the seam between a run and the mesh.

A workflow declares candidates (``{role: reviewer}``); turning those into
*sessions that exist right now* needs the mesh roster, the member graph and the
spawn tree, and only the daemon holds any of them. This module is where that
lookup lands, so the engine stays what it has always been: a state machine over
files.

The lookup is one call, not one per candidate. An ask resolves against a single
:class:`Pool` — one reading of the mesh — and every group is then matched
against that same snapshot. Groups are a preference order over one moment's
roster, not a sequence of questions about a moving one.

Three properties of the result matter more than its mechanism:

**It is frozen into the run.** Authorization afterwards is a membership test
against what was recorded — never a re-resolution. Same rule the mesh applies
to ``auto_link`` (evaluated at join, stored as an edge): a decision about who
may act must not change because a session exited or was spawned later.

**A candidate is never something the run made.** The pool excludes the asking
session and everything below it in the spawn tree, because those are exactly
what it could have manufactured — it can spawn children, and it can wire itself
to them (``SessionManager.commands`` runs strictly *down* the tree). It cannot
spawn a sibling and cannot wire itself to one, so siblings, uncles and roots
are as safe as ancestors. ``scope: ancestor`` narrows to the chain of command
for the workflows that want it; it is not what makes this sound.

**A responder must be local.** Answering means writing the asking run's state,
and those files live on the asking machine; a member on another daemon cannot
touch them. Remote matches are therefore skipped *with that reason* rather than
silently missed — the fix (ask someone here, or a human) is a different one
from "nobody holds that role".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Set, Tuple

from .. import daemon_client
from . import model

#: Ceiling on the daemon calls this module makes. They run while the run's
#: slot lock is held, so they are bounded well under the lock's own patience
#: (``state.LOCK_TIMEOUT``): the daemon is on localhost, and a run must not
#: become unoperable because it went quiet.
CALL_TIMEOUT = 5.0

#: Reachability values that mean the member is not there to answer. Anything
#: else — busy, idle, whatever the manager reports — is somebody who can.
GONE = ("exited", "missing")

#: Guard on the parent walk. It follows data that may have arrived over a
#: relay, so it must not be able to spin on a cycle a peer sent us.
MAX_DEPTH = 64


@dataclass(frozen=True)
class Responder:
    """One mesh member, as a possible answerer of one question."""

    #: The managed session name — the identity its MCP server reports when it
    #: answers, and what the recorded list is matched against.
    session: str
    #: Its mesh handle, for delivery and for anything a human reads.
    handle: str
    role: str
    #: Hosted by this daemon. A remote member is kept in the pool so it can be
    #: reported as skipped-because-remote rather than not found.
    local: bool = True
    #: What the daemon says about the session behind the handle.
    reachability: str = ""

    @property
    def answerable(self) -> bool:
        return bool(self.session) and self.local and self.reachability not in GONE

    def to_dict(self) -> dict:
        return {
            "kind": "member",
            "session": self.session,
            "handle": self.handle,
            "role": self.role,
        }


@dataclass(frozen=True)
class Pool:
    """Who the asking session could put a question to, at one moment.

    ``problem`` set means there is no pool to speak of — no daemon, no
    membership, an ambiguous mesh. That is not an error: every group then fails
    to match for that stated reason and the ask runs out of candidates, which
    hands it to ``otherwise`` — where an unanswerable question belongs.
    """

    mesh: str = ""
    #: The asking session's own handle.
    me: str = ""
    #: Every member of the mesh except the asking session itself, by handle.
    members: Dict[str, Responder] = field(default_factory=dict)
    #: Handles the asking session may message (its side of the member graph).
    reachable: Set[str] = field(default_factory=set)
    #: Handles below the asking session in the spawn tree — never candidates.
    descendants: Set[str] = field(default_factory=set)
    #: Handles above it, nearest first.
    ancestors: List[str] = field(default_factory=list)
    problem: str = ""

    def eligible(self, candidate: model.Candidate) -> List[Responder]:
        """Members this candidate's role and scope name, reachable or not."""
        if candidate.scope == model.SCOPE_ANCESTOR:
            handles = [h for h in self.ancestors if h in self.members]
        else:
            handles = [h for h in sorted(self.members) if h not in self.descendants]
        return [
            self.members[h] for h in handles if self.members[h].role == candidate.role
        ]

    def match(self, candidate: model.Candidate) -> Tuple[List[Responder], Optional[str]]:
        """Members this candidate names, or ``([], reason)``.

        The reason is what a human reads when the question lands in front of
        them, so it names the specific miss — wrong role, not wired, remote,
        exited — rather than reporting them all as "nobody". Each check is
        stated in the order that makes the next fix obvious: is there such a
        role at all, then can this run reach it, then can it answer.
        """
        where = candidate.describe()
        if self.problem:
            return [], f"{where}: {self.problem}"
        matched = self.eligible(candidate)
        if not matched:
            return [], f"{where}: {self._nobody_holds(candidate)}"
        reachable = [m for m in matched if m.handle in self.reachable]
        if not reachable:
            names = ", ".join(m.handle for m in matched)
            first = matched[0].handle
            return [], (
                f"{where}: {names} holds it but {self.me or 'this run'} is not "
                f"wired to them in mesh {self.mesh!r} — a session above them, "
                f"or a person, can run 'claunch mesh connect {self.me} {first}'"
            )
        answerable = [m for m in reachable if m.answerable]
        if not answerable:
            remote = [m.handle for m in reachable if not m.local or not m.session]
            if remote:
                return [], (
                    f"{where}: {', '.join(remote)} matched but cannot answer "
                    f"from here — a member on another daemon has no access to "
                    f"this run's state"
                )
            names = ", ".join(m.handle for m in reachable)
            return [], f"{where}: {names} matched but the session has exited"
        return answerable, None

    def _nobody_holds(self, candidate: model.Candidate) -> str:
        if candidate.scope == model.SCOPE_ANCESTOR:
            above = [
                f"{h} ({self.members[h].role})"
                for h in self.ancestors
                if h in self.members
            ]
            found = ", ".join(above) if above else "nothing"
            return (
                f"no session above {self.me or 'this run'} in mesh "
                f"{self.mesh!r} holds that role — found {found}"
            )
        others = [
            f"{h} ({self.members[h].role})"
            for h in sorted(self.members)
            if h not in self.descendants
        ]
        found = ", ".join(others) if others else "nobody"
        return (
            f"no member of mesh {self.mesh!r} holds that role — found {found} "
            f"(sessions this run spawned itself are never candidates)"
        )


def pool(*, session: str, mesh: str = "", cwd: Optional[str] = None) -> Pool:
    """Read who ``session`` could ask, or a :class:`Pool` saying why nobody.

    Never raises. Everything that can go wrong here — the daemon being down,
    the session not being enrolled, two meshes with no way to choose — is a
    reason a human should read, not a reason to take the run down.
    """
    if not session:
        return Pool(
            problem="this run is not driven by a managed session, so it has "
            "no mesh identity to ask from"
        )
    client = daemon_client.connect()
    if client is None:
        return Pool(
            problem="the claunch daemon is not running, so the mesh roster "
            "cannot be read"
        )
    try:
        doc = client.get("/api/mesh", timeout=CALL_TIMEOUT)
    except daemon_client.DaemonClientError as exc:
        return Pool(problem=f"the mesh roster could not be read: {exc}")

    found = [
        info
        for info in (doc.get("meshes") or [])
        if _handle_in(info, session)
        and (not mesh or str(info.get("name") or "") == mesh)
    ]
    if not found:
        if mesh:
            return Pool(problem=f"session {session!r} is not a member of mesh {mesh!r}")
        return Pool(
            problem=f"session {session!r} is not a member of any mesh on this "
            f"machine, so it can reach nobody — join one, or let the question "
            f"go to a human"
        )
    if len(found) > 1:
        names = ", ".join(sorted(str(i.get("name")) for i in found))
        return Pool(
            problem=f"session {session!r} is in several meshes ({names}) and "
            f"the run does not say which one to ask — start it with an "
            f"explicit mesh"
        )
    return _pool_from(found[0], session)


def _pool_from(info: dict, session: str) -> Pool:
    raw = _members(info)
    me = _handle_in(info, session)
    return Pool(
        mesh=str(info.get("name") or ""),
        me=me,
        members={
            handle: Responder(
                session=str(m.get("session") or ""),
                handle=handle,
                role=str(m.get("role") or ""),
                local=bool(m.get("local")),
                reachability=str(m.get("reachability") or ""),
            )
            for handle, m in raw.items()
            if handle != me
        },
        reachable=_reachable(info, me),
        descendants=_descendants(raw, me),
        ancestors=_ancestors(raw, me),
        problem="",
    )


def _members(info: dict) -> Dict[str, dict]:
    return {
        str(m.get("handle")): m
        for m in (info.get("members") or [])
        if m.get("handle")
    }


def _handle_in(info: dict, session: str) -> str:
    """This session's handle in ``info``, or "" — locally hosted only.

    A handle on a mirrored mesh may name a session on another machine, and
    session names are only unique per machine, so a roster match alone would
    happily identify us as somebody else's agent.
    """
    for handle, member in _members(info).items():
        if member.get("session") == session and member.get("local"):
            return handle
    return ""


def _reachable(info: dict, me: str) -> Set[str]:
    """Handles ``me`` may message, from the mesh's own member graph.

    Read from the published edge table rather than assumed, because that graph
    is the authorization the run cannot widen: only a session *above* it, or a
    person, may add an edge. A spawned member is wired to its parent alone, so
    a sibling reviewer is reachable exactly when somebody deliberately wired it.
    """
    out: Set[str] = set()
    if not me:
        return out
    for edge in info.get("member_links") or []:
        if not edge.get("enabled"):
            continue
        a, b = str(edge.get("a") or ""), str(edge.get("b") or "")
        if a == me and b:
            out.add(b)
        elif b == me and a:
            out.add(a)
    return out


def _ancestors(members: Dict[str, dict], me: str) -> List[str]:
    """Handles above ``me``, nearest first.

    Follows the ``parent`` the daemon publishes, which is already the nearest
    *enrolled* ancestor — a session in the middle of the tree that never joined
    is collapsed through rather than breaking the chain.
    """
    chain: List[str] = []
    seen = {me}
    current = me
    while current and len(chain) < MAX_DEPTH:
        parent = str((members.get(current) or {}).get("parent") or "")
        if not parent or parent in seen or parent not in members:
            break
        seen.add(parent)
        chain.append(parent)
        current = parent
    return chain


def _descendants(members: Dict[str, dict], me: str) -> Set[str]:
    """Every handle below ``me`` in the spawn tree — the excluded set.

    Computed by walking each member's parents up to ``me`` rather than down
    from it, because the published ``parent`` only points one way. Depth is
    bounded per member for the same reason the upward walk is.
    """
    out: Set[str] = set()
    if not me:
        return out
    for handle in members:
        if handle == me:
            continue
        if me in _ancestors(members, handle):
            out.add(handle)
    return out


def deliver(
    ask: dict,
    *,
    mesh: str,
    sender: str,
    workflow: str,
    to: List[str],
) -> Optional[str]:
    """Put the question in front of the responders. Returns a failure reason.

    Best-effort by design: a question that was recorded but not announced is
    still answerable (the responder's ``asks`` tool finds it, and a human can
    see it), whereas a run that refused to open an ask because a message did
    not send would be stuck on the least important half of the operation. The
    failure is returned so it can be journaled and shown, not swallowed.
    """
    client = daemon_client.connect()
    if client is None:
        return "the claunch daemon is not running, so nobody was notified"
    try:
        client.post(
            f"/api/mesh/{mesh}/messages",
            {
                "from": sender,
                "to": to,
                "body": _question(ask, workflow=workflow, sender=sender),
                # `decide`, not `ask`: the answer is recorded in the run, not
                # sent back down this thread, and the mesh's owed ledger closes
                # a debt on *any* message from the member — so an `ask` here
                # would let an unrelated reply read as "handled".
                "type": "decide",
                # Opaque to the mesh: enough for a reader to link to the run,
                # without the mesh learning cflow's schema.
                "ref": {
                    "kind": "cflow.ask",
                    "id": ask.get("id"),
                    "step": ask.get("step"),
                },
            },
            timeout=CALL_TIMEOUT,
        )
    except daemon_client.DaemonClientError as exc:
        return f"the question could not be delivered: {exc}"
    return None


def nudge(session: str, message: str, *, cwd: str) -> List[str]:
    """Type a resume nudge into the run's own session. Returns who was nudged.

    A run identifies its session by scope, but a scope is only unique within
    a directory, so both must match — the same pair that identifies the run
    itself. Best-effort: without a daemon there is nobody to type into, and a
    missed nudge costs a delay rather than correctness, since the protocol
    already makes an agent re-read ``status`` on any message.

    Goes through the daemon's ``/deliver`` — the same door in-process senders
    use — rather than typing keys, so how a message gets submitted stays one
    decision made in one place.
    """
    from . import state as state_mod  # local: state imports model, not us

    target = str(session or "").strip()
    if not target or target == state_mod.DEFAULT_SCOPE:
        return []
    client = daemon_client.connect()
    if client is None:
        return []
    try:
        sessions = (client.get("/api/sessions", timeout=CALL_TIMEOUT) or {}).get(
            "sessions"
        ) or []
    except daemon_client.DaemonClientError:
        return []
    want = state_mod.resolve_cwd(cwd)
    for s in sessions:
        if s.get("name") != target or s.get("status") == "exited":
            continue
        try:
            if state_mod.resolve_cwd(str(s.get("cwd") or "")) != want:
                continue
        except OSError:
            continue
        try:
            client.post(
                f"/api/sessions/{s['name']}/deliver",
                {"text": message},
                timeout=CALL_TIMEOUT,
            )
        except daemon_client.DaemonClientError:
            return []
        return [target]
    return []


def _question(ask: dict, *, workflow: str, sender: str) -> str:
    """The message a responder receives. The question — never the answer form.

    Deliberately not a template to fill in and send back: the answer is a tool
    call against a closed option set, so nothing a responder writes here has
    to be parsed. What the text has to do is state the decision, the options,
    and where to record one.
    """
    options = "\n".join(
        f"  - {o['name']}: {o.get('description') or ''}".rstrip()
        for o in ask.get("options") or []
    )
    lines = [
        f"{sender} needs a decision on workflow {workflow!r}, "
        f"step {ask.get('step')!r}.",
        "",
        str(ask.get("prompt") or "").strip(),
        "",
        "Answer with one of:",
        options,
        f"  - abstain: you have no basis to decide — passes it further up",
        "",
        f"Record it with the cflow 'answer' tool: "
        f"{{ask: {ask.get('id')!r}, decision: <one of the above>, reason: <why>}}. "
        f"Call 'asks' first if you need the full context. Judge it yourself "
        f"against the code — you were asked because {sender} does not get to "
        f"decide this one.",
    ]
    if ask.get("deadline"):
        lines.append(f"If you do not answer by {ask['deadline']}, it moves on without you.")
    return "\n".join(lines)
