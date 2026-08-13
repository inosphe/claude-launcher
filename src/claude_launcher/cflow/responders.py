"""Who can answer a delegated decision — the seam between a run and the mesh.

A workflow declares candidates (``{role: leader, up: 1}``); turning those into
*sessions that exist right now* needs the mesh roster and the spawn lineage,
and only the daemon holds either. This module is where that lookup lands, so
the engine stays what it has always been: a state machine over files.

The lookup is one call, not one per candidate. An ask resolves against a
single :class:`Ancestry` — the chain above the asking session, read once — and
every group is then matched against that same snapshot. Groups are a
preference order over one moment's roster, not a sequence of questions about
a moving one.

Three properties of the result matter more than its mechanism:

**It is frozen into the run.** Authorization afterwards is a membership test
against what was recorded — never a re-resolution. Same rule the mesh applies
to ``auto_link`` (evaluated at join, stored as an edge): a decision about who
may act must not change because a session exited or was spawned later.

**A candidate is always an ancestor.** ``up`` is required by the schema for
this reason, and the chain is built by walking ``parent`` edges upward, so
there is no direction in which a run could reach something it spawned.

**A responder must be local.** Answering means writing the asking run's state,
and those files live on the asking machine; a member on another daemon cannot
touch them. Remote matches are therefore skipped *with that reason* rather
than silently missed — the fix (ask someone here, or a human) is a different
one from "nobody holds that role".
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Tuple

from .. import daemon_client
from . import model

#: Ceiling on the daemon calls this module makes. They run while the run's
#: slot lock is held, so they are bounded well under the lock's own patience
#: (``state.LOCK_TIMEOUT``): the daemon is on localhost, and a run must not
#: become unoperable because it went quiet.
CALL_TIMEOUT = 5.0


@dataclass(frozen=True)
class Responder:
    """One member of the asking session's lineage, as a possible answerer."""

    #: The managed session name — the identity its MCP server reports when it
    #: answers, and what the recorded list is matched against.
    session: str
    #: Its mesh handle, for delivery and for anything a human reads.
    handle: str
    role: str
    #: Hops above the asking session in the (enrolled) spawn lineage.
    up: int
    #: Hosted by this daemon. A remote member is kept in the chain so it can
    #: be reported as skipped-because-remote rather than not found.
    local: bool = True

    def to_dict(self) -> dict:
        return {
            "kind": "member",
            "session": self.session,
            "handle": self.handle,
            "role": self.role,
            "up": self.up,
        }


#: What an ``asked`` entry looks like for the human token. A human holds no
#: session and no handle, so there is nothing to record but the kind: their
#: authority comes from reaching the CLI or the dashboard at all.
HUMAN_ENTRY = {"kind": "human"}


@dataclass(frozen=True)
class Ancestry:
    """The chain above one session at one moment, and how to match it.

    ``problem`` set means there is no chain to speak of — no daemon, no
    membership, an ambiguous mesh. It is not an error: every group then fails
    to match for that stated reason and the ask escalates to the human at the
    end of the list, which is where an unanswerable question belongs.
    """

    mesh: str = ""
    #: The asking session's own handle.
    me: str = ""
    #: Ancestors nearest-first (``up`` 1, 2, ...).
    chain: List[Responder] = field(default_factory=list)
    problem: str = ""

    def match(self, candidate: model.Candidate) -> Tuple[List[Responder], Optional[str]]:
        """Members this candidate names, or ``([], reason)``.

        The reason is written for whoever the question lands in front of when
        nothing matched, so it names the specific miss — out of range, wrong
        role, remote — rather than reporting them all as "nobody".
        """
        where = candidate.describe()
        if self.problem:
            return [], f"{where}: {self.problem}"
        low, high = candidate.up  # type: ignore[misc]
        in_range = [a for a in self.chain if low <= a.up <= high]
        if not in_range:
            depth = len(self.chain)
            return [], (
                f"{where}: the lineage above {self.me or 'this run'} in mesh "
                f"{self.mesh!r} is {depth} deep, so nothing sits that far up"
            )
        if candidate.role is None:
            matched = in_range
        else:
            matched = [a for a in in_range if a.role == candidate.role]
            if not matched:
                held = ", ".join(f"{a.handle} ({a.role})" for a in in_range)
                return [], (
                    f"{where}: nobody in range holds that role — found {held}"
                )
        answerable = [a for a in matched if a.local and a.session]
        if not answerable:
            names = ", ".join(a.handle for a in matched)
            return [], (
                f"{where}: {names} matched but cannot answer from here — a "
                f"member on another daemon has no access to this run's state"
            )
        return answerable, None


def ancestry(*, session: str, mesh: str = "", cwd: Optional[str] = None) -> Ancestry:
    """Read the lineage above ``session``, or an :class:`Ancestry` saying why not.

    Never raises. Everything that can go wrong here — the daemon being down,
    the session not being enrolled, two meshes with no way to choose — is a
    reason a human should read, not a reason to take the run down.
    """
    if not session:
        return Ancestry(
            problem="this run is not driven by a managed session, so it has "
            "no lineage to look up"
        )
    client = daemon_client.connect()
    if client is None:
        return Ancestry(
            problem="the claunch daemon is not running, so the mesh roster "
            "cannot be read"
        )
    try:
        doc = client.get("/api/mesh", timeout=CALL_TIMEOUT)
    except daemon_client.DaemonClientError as exc:
        return Ancestry(problem=f"the mesh roster could not be read: {exc}")

    found = [
        info
        for info in (doc.get("meshes") or [])
        if _handle_in(info, session)
        and (not mesh or str(info.get("name") or "") == mesh)
    ]
    if not found:
        if mesh:
            return Ancestry(
                problem=f"session {session!r} is not a member of mesh {mesh!r}"
            )
        return Ancestry(
            problem=f"session {session!r} is not a member of any mesh on this "
            f"machine, so it has no lineage — join one, or let the question "
            f"go to a human"
        )
    if len(found) > 1:
        names = ", ".join(sorted(str(i.get("name")) for i in found))
        return Ancestry(
            problem=f"session {session!r} is in several meshes ({names}) and "
            f"the run does not say which one to ask — start it with an "
            f"explicit mesh"
        )
    info = found[0]
    me, chain = _walk_up(info, session)
    return Ancestry(mesh=str(info.get("name") or ""), me=me, chain=chain)


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


def _walk_up(info: dict, session: str) -> Tuple[str, List[Responder]]:
    """The asking handle and its ancestors, nearest first.

    Follows the ``parent`` the daemon publishes, which is already the nearest
    *enrolled* ancestor — a session in the middle of the tree that never
    joined is collapsed through rather than breaking the chain. Guarded
    against a cycle and bounded by the schema's own reach, because this walks
    data that arrives over a relay.
    """
    members = _members(info)
    me = _handle_in(info, session)
    chain: List[Responder] = []
    seen = {me}
    current = me
    while current and len(chain) < model.MAX_UP:
        parent = str((members.get(current) or {}).get("parent") or "")
        if not parent or parent in seen or parent not in members:
            break
        seen.add(parent)
        member = members[parent]
        chain.append(
            Responder(
                session=str(member.get("session") or ""),
                handle=parent,
                role=str(member.get("role") or ""),
                up=len(chain) + 1,
                local=bool(member.get("local")),
            )
        )
        current = parent
    return me, chain


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
                # An intent that invites a reply, so an unanswered question
                # shows up in the mesh's own owed ledger too — the nudger and
                # the dashboard already chase those.
                "type": "ask",
            },
            timeout=CALL_TIMEOUT,
        )
    except daemon_client.DaemonClientError as exc:
        return f"the question could not be delivered: {exc}"
    return None


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
