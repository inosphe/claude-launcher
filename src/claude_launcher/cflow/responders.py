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

**A candidate is never something the run made — unless the workflow says so.**
The pool excludes the asking session and everything below it in the spawn
tree, because those are exactly what it could have manufactured — it can spawn
children, and it can wire itself to them (``SessionManager.commands`` runs
strictly *down* the tree). It cannot spawn a sibling and cannot wire itself to
one, so siblings, uncles and roots are as safe as ancestors. ``scope:
ancestor`` narrows to the chain of command for the workflows that want it; it
is not what makes this sound. ``scope: descendant`` is the one declared way
back in: a file a person reviews says that this decision may be staffed by
the run itself (a review that would otherwise pass unreviewed), and no other
scope ever reaches below the asking session.

**Hierarchy.** Every other member stands in exactly one relation to the asking
session, read off the spawn tree the daemon publishes (each member's
``parent`` is its nearest *enrolled* ancestor):

- *ancestor* — on its parent chain;
- *descendant* — it is on theirs;
- *sibling* — same parent. Members with no parent in the mesh are the roots
  the operator started, and they are siblings of one another: they share the
  one parent that is not a session;
- *collateral* — none of those: an uncle, a cousin, a sibling's child. No
  scope but ``any`` names them. A reviewer another worker spawned for its own
  work is that worker's, and a narrower scope must never hand it somebody
  else's question.

**An edge may be made, never widened past the roster.** A candidate that
declares ``connect: true`` turns "holds the role but is not wired to this run"
from a skip into an edge — see :func:`wire`. That is the one thing here the
run could not do for itself, so it is done with the daemon's own authority and
bounded to exactly that miss: the candidate must already name a member of this
mesh that holds the declared role and can answer. Nothing about who is *in* the
pool changes, which is what keeps the paragraph above true — a session the run
spawned is still excluded, and wiring cannot reach one.

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
    #: Its primary role — what the roster prints and what its stance is.
    role: str
    #: Every role it HOLDS, primary first, then its subroles. A candidate's
    #: role is matched against this, so a leader that took ``reviewer`` as
    #: a subrole answers a ``{role: reviewer}`` question. Empty reads as the
    #: primary alone (a roster from a daemon that predates subroles).
    roles: Tuple[str, ...] = ()
    #: Hosted by this daemon. A remote member is kept in the pool so it can be
    #: reported as skipped-because-remote rather than not found.
    local: bool = True
    #: What the daemon says about the session behind the handle.
    reachability: str = ""

    @property
    def answerable(self) -> bool:
        return bool(self.session) and self.local and self.reachability not in GONE

    @property
    def held(self) -> Tuple[str, ...]:
        return self.roles or (self.role,)

    def holds(self, role: str) -> bool:
        return role in self.held

    def role_label(self) -> str:
        """``leader+reviewer`` — how a skip reason names what somebody is."""
        return "+".join(self.held)

    def to_dict(self) -> dict:
        return {
            "kind": "member",
            "session": self.session,
            "handle": self.handle,
            "role": self.role,
            "roles": list(self.held),
        }


@dataclass(frozen=True)
class Pool:
    """Who the asking session could put a question to, at one moment.

    ``problem`` set means there is no pool to speak of — no daemon, no
    membership, an ambiguous mesh. That is not an error: every group then fails
    to match for that stated reason and the ask runs out of candidates, which
    hands it to ``otherwise`` — where an unanswerable question belongs.

    ``unreadable`` splits that set in two, and the split is the whole reason
    it exists. Most problems here are *answers*: this session is in no mesh,
    or in several, or no daemon is running at all — facts read off the record,
    which will not be different a minute from now. One is not an answer: a
    daemon that is announced and alive and did not reply inside a short budget
    (:data:`.daemon_client.UNRESPONSIVE`). From out here busy and stuck look
    identical, which is why :func:`.daemon_client.unreachable_reason` refuses
    to phrase that silence as an absence — and a caller that spends something
    irreversible on the pool must refuse it too.
    """

    mesh: str = ""
    #: The asking session's own handle.
    me: str = ""
    #: The asking session's own mesh role, as resolved and stored at its join.
    #: Read for the workflow's ``filter_roles`` check; empty when ``me`` is.
    me_role: str = ""
    #: Every role the asking session holds — ``me_role`` first, then its
    #: subroles. ``filter_roles`` is held against all of them (see
    #: :meth:`.model.RoleFilter.allows_any`). Empty reads as ``me_role`` alone.
    me_roles: Tuple[str, ...] = ()
    #: Every member of the mesh except the asking session itself, by handle.
    members: Dict[str, Responder] = field(default_factory=dict)
    #: Handles the asking session may message (its side of the member graph).
    reachable: Set[str] = field(default_factory=set)
    #: Handles below the asking session in the spawn tree — candidates only
    #: for ``scope: descendant``.
    descendants: Set[str] = field(default_factory=set)
    #: Handles above it, nearest first.
    ancestors: List[str] = field(default_factory=list)
    #: Handles sharing its parent — or, when it has none, the other roots.
    siblings: Set[str] = field(default_factory=set)
    #: Depth below the asking session, per descendant (child = 1), so a
    #: ``descendant`` group lists its own children before theirs.
    generation: Dict[str, int] = field(default_factory=dict)
    problem: str = ""
    #: The ``problem`` above is silence rather than a fact — the roster could
    #: not be read this time and may read fine on the next. Callers that spend
    #: something they cannot get back (the engine spends a candidate group per
    #: ask, permanently) must wait for an answer instead of treating this as
    #: one. See the class docstring.
    unreadable: bool = False
    #: Handles this run wired itself to, because a candidate declared
    #: ``connect: true``. Read by the engine so the ask records that the edge
    #: was made by a workflow's declaration rather than by a person — the
    #: mesh's own edge table stores no such attribution, and an edge nobody
    #: can tell from a deliberate one erases the judgement that made it.
    wired: List[str] = field(default_factory=list)

    def relation(self, handle: str) -> str:
        """``ancestor`` / ``descendant`` / ``sibling`` / ``collateral`` — how
        ``handle`` stands to the asking session. See "Hierarchy" above."""
        if handle in self.ancestors:
            return "ancestor"
        if handle in self.descendants:
            return "descendant"
        if handle in self.siblings:
            return "sibling"
        return "collateral"

    def in_scope(self, scope: str) -> List[str]:
        """Handles ``scope`` admits, in the order a group lists them."""
        if scope == model.SCOPE_ANCESTOR:
            return [h for h in self.ancestors if h in self.members]
        if scope == model.SCOPE_DESCENDANT:
            return sorted(
                (h for h in self.descendants if h in self.members),
                key=lambda h: (self.generation.get(h, 0), h),
            )
        if scope == model.SCOPE_SIBLING:
            return sorted(h for h in self.siblings if h in self.members)
        return [h for h in sorted(self.members) if h not in self.descendants]

    def eligible(self, candidate: model.Candidate) -> List[Responder]:
        """Members this candidate's role and scope name, reachable or not."""
        return [
            self.members[h]
            for h in self.in_scope(candidate.scope)
            if self.members[h].holds(candidate.role)
        ]

    def match(
        self, candidate: model.Candidate, *, autowire: bool = False
    ) -> Tuple[List[Responder], Optional[str]]:
        """Members this candidate names, or ``([], reason)``.

        The reason is what a human reads when the question lands in front of
        them, so it names the specific miss — wrong role, not wired, remote,
        exited — rather than reporting them all as "nobody". Each check is
        stated in the order that makes the next fix obvious: is there such a
        role at all, then can this run reach it, then can it answer.

        ``autowire`` is the caller's permission to have side effects, and is
        off by default so that reading who *would* answer stays a read. Only
        the one caller that is actually opening a question passes it; the
        preview reports the missing edge and says it will be made.
        """
        where = candidate.describe()
        if self.problem:
            return [], f"{where}: {self.problem}"
        matched = self.eligible(candidate)
        if not matched:
            return [], f"{where}: {self._nobody_holds(candidate)}"
        reachable = [m for m in matched if m.handle in self.reachable]
        # The note is built here, where both facts are known: whether wiring
        # was allowed to run at all, and what it did. Two readers need
        # different halves. Where nothing was attempted — the preview, which
        # reports who *would* answer — the missing edge should not read as
        # something to go and add by hand, because it is about to be made.
        # Where the attempt was made and did not take, the failure itself is
        # the useful half, and "add it by hand" is the wrong instruction.
        note = ""
        if not reachable and candidate.connect:
            if not autowire:
                note = (
                    " — this candidate declares 'connect: true', so the edge "
                    "is made when the question is actually opened"
                )
            else:
                reachable, failures = self._wire_to(matched)
                if failures:
                    note = (
                        " — this candidate declares 'connect: true' and the "
                        f"edge could not be made: {'; '.join(failures)}"
                    )
        if not reachable:
            names = ", ".join(m.handle for m in matched)
            first = matched[0].handle
            return [], (
                f"{where}: {names} holds it but {self.me or 'this run'} is not "
                f"wired to them in mesh {self.mesh!r} — a session above them, "
                f"or a person, can run 'claunch mesh connect {self.me} {first}'"
                + note
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

    def _wire_to(
        self, matched: List[Responder]
    ) -> Tuple[List[Responder], List[str]]:
        """Make the missing edges this candidate declared. Returns who became
        reachable, and the attempts that failed.

        **Bounded, and not a loop.** One attempt per member, made here and
        never re-entered: ``match`` calls this once, uses whatever came back,
        and falls through to its ordinary reasons. So the work is at most one
        daemon call per member the candidate matched, and every outcome —
        every edge made, none made, some made — lands on one of the same
        reason lines a run without ``connect`` would have produced. There is
        no path back to this method from its own result.

        Only members that could actually answer are wired. A candidate's
        matches include the remote and the exited, and an edge to one of those
        buys nothing while leaving a link behind in the roster that outlives
        the run — so the miss reported for them stays the specific one
        (``remote``/``exited``), which is a different fix from "not wired".

        The pool's ``reachable`` set is updated in place on success, so a later
        group naming the same member does not make the edge twice. Failures
        are returned rather than raised: a wiring that did not take is one more
        thing the reader of the skip needs, and not a reason to take a run down.
        """
        if not self.me or not self.mesh:
            return [], []
        wired: List[Responder] = []
        failures: List[str] = []
        for member in [m for m in matched if m.answerable]:
            failure = wire(self.mesh, self.me, member.handle)
            if failure:
                failures.append(f"{member.handle}: {failure}")
                continue
            self.reachable.add(member.handle)
            self.wired.append(member.handle)
            wired.append(member)
        return wired, failures

    def _nobody_holds(self, candidate: model.Candidate) -> str:
        if candidate.scope != model.SCOPE_ANY:
            who = self.me or "this run"
            where = {
                model.SCOPE_ANCESTOR: f"no session above {who}",
                model.SCOPE_SIBLING: f"no sibling of {who}",
                model.SCOPE_DESCENDANT: f"no session {who} spawned",
            }[candidate.scope]
            inside = self.in_scope(candidate.scope)
            listed = [f"{h} ({self.members[h].role_label()})" for h in inside]
            found = ", ".join(listed) if listed else "nothing"
            # Holders in another relation are named with it: "a reviewer
            # exists but is a cousin" has a different fix (staff your own)
            # from "nobody holds the role at all".
            elsewhere = [
                f"{h} ({self.relation(h)})"
                for h, m in sorted(self.members.items())
                if h not in inside and m.holds(candidate.role) and m.answerable
            ]
            tail = (
                f"; held outside this scope by {', '.join(elsewhere)}"
                if elsewhere
                else ""
            )
            return (
                f"{where} in mesh {self.mesh!r} holds that role — found "
                f"{found}{tail}"
            )
        others = [
            f"{h} ({self.members[h].role_label()})"
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
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        # Which of the two it was decides what the reader does next, and a
        # slow daemon reported as an absent one sends them to start a second.
        return Pool(
            problem=f"the claunch {daemon_client.unreachable_reason(why)}, "
            f"so the mesh roster cannot be read",
            # Absence is established from the record (nothing announced, or a
            # pid that is gone) and is a fact. Anything else is a live process
            # that stayed quiet, and `is_absent` is the predicate that already
            # knows the difference — read it here rather than re-deciding it.
            unreadable=not daemon_client.is_absent(why),
        )
    try:
        doc = client.get("/api/mesh", timeout=CALL_TIMEOUT)
    except daemon_client.DaemonClientError as exc:
        # The health probe answered, so there IS a daemon; this call did not.
        return Pool(
            problem=f"the mesh roster could not be read: {exc}", unreadable=True
        )

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
        me_role=str((raw.get(me) or {}).get("role") or ""),
        me_roles=_roles_of(raw.get(me) or {}),
        members={
            handle: Responder(
                session=str(m.get("session") or ""),
                handle=handle,
                role=str(m.get("role") or ""),
                roles=_roles_of(m),
                local=bool(m.get("local")),
                reachability=str(m.get("reachability") or ""),
            )
            for handle, m in raw.items()
            if handle != me
        },
        reachable=_reachable(info, me),
        descendants=_descendants(raw, me),
        ancestors=_ancestors(raw, me),
        siblings=_siblings(raw, me),
        generation=_generations(raw, me),
        problem="",
    )


def _roles_of(member: dict) -> Tuple[str, ...]:
    """The roles a roster row holds: its ``roles`` list when the daemon
    publishes one, else the primary alone (an older daemon, or none)."""
    primary = str(member.get("role") or "")
    listed = member.get("roles")
    if isinstance(listed, list) and listed:
        names = [str(r) for r in listed if str(r or "").strip()]
        if names:
            return tuple(dict.fromkeys(names))
    return (primary,) if primary else ()


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


def _parent_of(members: Dict[str, dict], handle: str) -> str:
    """``handle``'s parent in this roster, or "" for a root.

    A parent that is not a member reads as none — where the upward walk in
    :func:`_ancestors` stops — so the two never disagree about where a tree
    begins.
    """
    parent = str((members.get(handle) or {}).get("parent") or "")
    return parent if parent in members and parent != handle else ""


def _siblings(members: Dict[str, dict], me: str) -> Set[str]:
    """Handles sharing ``me``'s parent. Roots share the operator, so a root's
    siblings are the other roots."""
    if not me:
        return set()
    mine = _parent_of(members, me)
    return {h for h in members if h != me and _parent_of(members, h) == mine}


def _generations(members: Dict[str, dict], me: str) -> Dict[str, int]:
    """Depth below ``me`` of every descendant: 1 for a child, 2 for its child."""
    out: Dict[str, int] = {}
    if not me:
        return out
    for handle in members:
        if handle == me:
            continue
        chain = _ancestors(members, handle)
        if me in chain:
            out[handle] = chain.index(me) + 1
    return out


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


def wire(mesh: str, me: str, other: str) -> Optional[str]:
    """Connect ``me`` to ``other`` with the daemon's own authority. Returns a
    failure reason.

    **Why this bypasses the mesh's ordinary rule.** A member may only edit an
    edge touching a session it commands — its own children and their
    descendants (``MeshManager.set_member_link``, guarded by
    ``_require_member_authority`` when an ``actor`` is named). That rule is
    what stops a spawned session from wiring its way out of the supervision it
    was spawned under, and it is exactly why a worker cannot reach a sibling
    reviewer: the reviewer is not below it. So the ask posts **without**
    ``actor``, which is the same authority a person at the CLI holds.

    **What bounds it.** The bypass is not "a run may wire itself to anyone".
    Four things have to be true at once before this is called, and each one is
    checked somewhere the run does not control:

    1. The step's own workflow declares ``connect: true`` on that candidate —
       a file, not a runtime choice, and one a person reviews.
    2. The candidate names a role, and a member of this mesh already holds it.
       Wiring never adds a member, and cannot invent a responder.
    3. That member is not one this run spawned unless the candidate says
       ``scope: descendant`` (``Pool.eligible`` removes descendants from every
       other scope before anything here runs), so a run still cannot
       manufacture its own approver where the workflow did not allow it — the
       property this module rests on. A descendant is wired to its parent
       from birth, so for that scope this bypass has nothing to add.
    4. The member is local and alive, so the edge is one a question can
       actually travel down.

    **What it does not decide.** It wires the asking run to a responder, and
    nothing else to anything: two reviewers that were deliberately left apart
    stay apart, because neither is an endpoint of the edge this makes. The
    handle is recorded on the pool (``Pool.wired``) and journaled by the
    engine, so an edge that appeared without a person's judgement behind it
    can be told from one that had it.
    """
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        return f"the claunch {daemon_client.unreachable_reason(why)}"
    try:
        client.patch(
            f"/api/mesh/{mesh}/members/{me}/links/{other}",
            # No `actor`: see the docstring. Naming one would apply the
            # session-tree check that this path exists to stand outside of,
            # and would refuse every edge worth making here.
            {"enabled": True},
            timeout=CALL_TIMEOUT,
        )
    except daemon_client.DaemonClientError as exc:
        return str(exc)
    return None


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
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        return (
            f"the claunch {daemon_client.unreachable_reason(why)}, so nobody "
            f"was notified"
        )
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


def withdraw(
    ask: dict,
    *,
    mesh: str,
    sender: str,
    workflow: str,
    to: List[str],
    decision: str,
) -> Optional[str]:
    """Tell the responders a person answered the question out from under them.

    The engine has always let a human settle an ask that was out with an
    agent, and the responder found out by having its answer refused —
    a whole turn spent on a decision that was no longer anyone's to make.
    This is the other half of that rule: the override is announced to the
    people it overrode, in the same thread the question arrived in.

    Best-effort and silent about it, like :func:`deliver`: the answer is
    already recorded and the run has already moved, so a message that does
    not send costs a wasted turn, not correctness.
    """
    client, why = daemon_client.connect_with_diagnosis()
    if client is None:
        return (
            f"the claunch {daemon_client.unreachable_reason(why)}, so nobody "
            f"was told"
        )
    body = (
        f"cflow: the question I sent you ({ask.get('id')}, "
        f"{workflow}/{ask.get('step')}) is closed — the user answered it "
        f"themselves: {decision!r}. A person's answer lands over a "
        f"responder's, so there is nothing left to decide here. Drop it and "
        f"carry on with your own work; do not answer it."
    )
    try:
        client.post(
            f"/api/mesh/{mesh}/messages",
            {
                "from": sender,
                "to": to,
                "body": body,
                # `fyi`: this closes a debt rather than opening one — the
                # responder owes nothing now, and the mesh must not chase it
                # for an answer to a question that no longer exists.
                "type": "fyi",
                "ref": {
                    "kind": "cflow.ask.withdrawn",
                    "id": ask.get("id"),
                    "step": ask.get("step"),
                },
            },
            timeout=CALL_TIMEOUT,
        )
    except daemon_client.DaemonClientError as exc:
        return f"the withdrawal could not be delivered: {exc}"
    return None


def nudge(session: str, message: str, *, cwd: str) -> List[str]:
    """Queue a resume nudge for the run's own session. Return accepted targets.

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
            result = client.post(
                f"/api/sessions/{s['name']}/deliver",
                {"text": message, "defer": True},
                timeout=CALL_TIMEOUT,
            )
        except daemon_client.DaemonClientError:
            return []
        return [target] if result.get("queued") or result.get("delivered") else []
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
