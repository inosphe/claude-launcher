"""Wire requests: the refusal that asks for a channel instead of a relay.

A mesh is a tree by default (``mesh_roles.AutoLink`` ships one rule, root to
root), so a send to a peer nobody wired is **refused** — and until now the
refusal told the sender to *go through its parent*:

    'w1' has no connection to w2 in mesh 'dev' — it can reach: lead. Ask the
    session that spawned you to connect you, or route through a peer you share.

That sentence is where the traffic comes from. "Route through a peer you
share" is an instruction to relay, and a relay costs four injections and two
of the middleman's turns for one peer-to-peer exchange (A->L, L->B, B->L,
L->A) where a channel costs one. Worse than the traffic — measured on
``mesh-0826``, 93.1% of 1256 injections touched the leader — is what the
middle does to the content: an agent asked to carry another agent's
*observation* restates it without the code in front of it, and the mesh reads
the restatement as the leader's own. That happened, to nine recipients at
once, and had to be walked back by measurement.

So the refusal stops being a dead end and becomes the signal. It is the best
signal available anywhere in the system, for a reason worth stating: it is
the one moment an agent says, unprompted and *by name*, that it needs a
specific peer. Nothing has to guess from timing, correlate a send with a
later send, or read a body to find it.

Four things this deliberately does NOT do:

* **It does not weaken the ACL.** The send is still refused. A request is
  filed and the message is not delivered — the graph decides who may speak,
  exactly as before, and only an authorized grant changes it.
* **It does not invent an approver.** The one session that may open the edge
  is already pinned by ``MeshManager._require_member_authority``: an agent
  edits only edges touching a session it commands. :func:`approver` picks
  from that same set, nearest first, so a grant never needs a rule the graph
  did not already have.
* **It does not broadcast.** One notice to one session. A request seen by
  everybody is the fan-out this module exists to remove.
* **It does not ask.** The notice is typed ``decide``: a decision is required
  and it is *recorded somewhere other than the thread* — the grant is a
  ``connect`` call, not a reply to parse. ``ask`` would park the notice in
  the owed ledger, where any later unrelated message from the approver would
  close it and the dashboard would report a decision nobody made. That is the
  same reading cflow's delegated decisions settled on (see
  ``docs/mesh-design.md``, "Delegated decisions ride the member graph").
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional, Tuple

#: Least time between two notices about the SAME pair. A refused agent
#: retries — that is the correct thing for it to do, and the retry is how the
#: record learns the need did not go away — but each retry must not spend
#: another of the approver's turns. The count keeps climbing while the mesh
#: stays quiet.
RENOTIFY_AFTER = 300.0

#: How many requests one mesh keeps. Settled ones (granted/declined) are
#: dropped first and oldest-first, so a mesh under pressure keeps the open
#: ones — the only ones anybody can still act on.
MAX_REQUESTS = 64

#: The states a request moves through. ``declined`` is terminal on purpose:
#: see :meth:`WireRequest.silent`.
OPEN, GRANTED, DECLINED = "open", "granted", "declined"


def pair_key(a: str, b: str) -> str:
    """The unordered key for a pair of handles.

    Identical to ``Mesh.member_key`` by construction: a request is about the
    same undirected edge the graph stores, and two keys for one edge would be
    two answers to "is this pair already asked for".
    """
    return "|".join(sorted((str(a), str(b))))


@dataclass
class WireRequest:
    """One member's standing ask for a channel to another.

    ``by`` is kept alongside the unordered pair because the two ends are not
    symmetric in one respect that matters: the requester is who gets told
    when the request settles. The edge is undirected; the disappointment is
    not.
    """

    a: str
    b: str
    by: str
    at: float
    count: int = 1
    state: str = OPEN
    decided_by: str = ""
    decided_at: float = 0.0
    reason: str = ""
    #: When the approver was last told. 0.0 = never (nobody to tell, or the
    #: notice failed), which is what lets a later retry try again.
    notified_at: float = 0.0
    #: The handle the notice went to, for the dashboard and for the sentence
    #: the requester is shown.
    approver: str = ""

    @property
    def key(self) -> str:
        return pair_key(self.a, self.b)

    @property
    def other(self) -> str:
        """The end that is not the requester."""
        return self.b if self.by == self.a else self.a

    def silent(self, now: float) -> bool:
        """Whether a fresh request on this pair should send no notice.

        Two reasons, and the second is the one that needs saying. Inside
        :data:`RENOTIFY_AFTER` the approver has already been told and has not
        had time to act. Once **declined**, it is told nothing ever again:
        the approver looked at this pair and said no, and a refused agent
        retries by nature — re-notifying on every retry would turn one "no"
        into an unbounded stream of the same question. The decline is not a
        cage, it is just quiet: an approver that changes its mind runs
        ``connect``, which grants.
        """
        if self.state == DECLINED:
            return True
        if self.state == GRANTED:
            return True
        return bool(self.notified_at) and (now - self.notified_at) < RENOTIFY_AFTER

    def to_dict(self) -> dict:
        return {
            "a": self.a,
            "b": self.b,
            "by": self.by,
            "at": self.at,
            "count": self.count,
            "state": self.state,
            "decided_by": self.decided_by,
            "decided_at": self.decided_at,
            "reason": self.reason,
            "notified_at": self.notified_at,
            "approver": self.approver,
        }

    @classmethod
    def from_dict(cls, doc: dict) -> Optional["WireRequest"]:
        """Rebuild one, or ``None`` for a row that cannot be trusted.

        Shape-tolerant like every other loader here: mesh.json is written by
        a daemon but read by whatever version comes next, and one unusable
        row must not sink the mesh it is in.
        """
        try:
            a, b, by = str(doc["a"]), str(doc["b"]), str(doc["by"])
        except (KeyError, TypeError):
            return None
        if not a or not b or a == b:
            return None
        state = str(doc.get("state") or OPEN)
        if state not in (OPEN, GRANTED, DECLINED):
            state = OPEN
        return cls(
            a=a,
            b=b,
            by=by,
            at=_num(doc.get("at")),
            count=max(1, int(_num(doc.get("count")) or 1)),
            state=state,
            decided_by=str(doc.get("decided_by") or ""),
            decided_at=_num(doc.get("decided_at")),
            reason=str(doc.get("reason") or ""),
            notified_at=_num(doc.get("notified_at")),
            approver=str(doc.get("approver") or ""),
        )


def _num(value) -> float:
    try:
        return float(value or 0.0)
    except (TypeError, ValueError):
        return 0.0


def load(doc) -> Dict[str, WireRequest]:
    """Rebuild a mesh's request table from its persisted mapping."""
    out: Dict[str, WireRequest] = {}
    if not isinstance(doc, dict):
        return out
    for row in doc.values():
        if not isinstance(row, dict):
            continue
        req = WireRequest.from_dict(row)
        if req is not None:
            out[req.key] = req
    return out


def dump(table: Dict[str, WireRequest]) -> dict:
    return {k: r.to_dict() for k, r in sorted(table.items())}


def trim(table: Dict[str, WireRequest]) -> None:
    """Bound the table in place, dropping settled requests before open ones.

    Trimmed on write rather than on a timer, for the reason
    :meth:`MeshManager.refusals` trims on read: nothing else in the daemon
    wakes up for a wire request, and a table nobody is looking at does not
    need to be tidy on a schedule.
    """
    if len(table) <= MAX_REQUESTS:
        return
    settled = sorted(
        (r for r in table.values() if r.state != OPEN),
        key=lambda r: r.decided_at or r.at,
    )
    for req in settled:
        if len(table) <= MAX_REQUESTS:
            return
        table.pop(req.key, None)
    for req in sorted(table.values(), key=lambda r: r.at):
        if len(table) <= MAX_REQUESTS:
            return
        table.pop(req.key, None)


def approver(
    requester: str,
    target: str,
    *,
    ancestors_of: Callable[[str], List[str]],
    handle_of: Callable[[str], str],
) -> Tuple[str, str]:
    """Which session should be asked to open ``requester`` <-> ``target``.

    Returns ``(handle, kind)``; ``("", "")`` when nobody on this daemon can
    grant it. ``kind`` is ``"common"`` for the nearest common ancestor and
    ``"parent"`` for an ancestor of the requester alone.

    Both arguments are SESSION names, not handles: authority is a property of
    the session tree, and a handle is only the name a session wears inside
    one mesh.

    The search is nearest-first up the requester's own line, and it stops at
    the first ancestor that is also a member of this mesh — an approver with
    no terminal here cannot be told. Preferring a **common** ancestor is not
    an extra rule, it is the one that makes the grant uncontroversial: a
    session that commands both ends is choosing how its own subtree talks to
    itself. Falling back to an ancestor of the requester alone stays inside
    what ``_require_member_authority`` already permits (an edge touching a
    session it commands), and is what lets a request cross between two trees
    at all.

    ``handle_of`` returns ``""`` for a session that is not a member here,
    which is also how a remote or departed ancestor drops out of the search.
    """
    line = list(ancestors_of(requester))
    if not line:
        return "", ""
    target_line = set(ancestors_of(target))
    for name in line:
        if name in target_line and handle_of(name):
            return handle_of(name), "common"
    for name in line:
        if handle_of(name):
            return handle_of(name), "parent"
    return "", ""


# --------------------------------------------------------------------------- #
# the sentences
#
# Kept here beside the state they describe rather than at their call sites:
# the requester's sentence and the approver's have to agree about what was
# filed and who holds it, and two strings built in two modules drift.
# --------------------------------------------------------------------------- #
def notice_body(req: WireRequest, mesh: str, *, kind: str) -> str:
    """What the approver is told. One decision, and the call that makes it."""
    who = (
        "both of them are in your subtree"
        if kind == "common"
        else f"{req.by} is in your subtree"
    )
    repeat = (
        f" It has asked {req.count} times."
        if req.count > 1 else ""
    )
    return (
        f"wire request: {req.by} tried to message {req.other} in mesh "
        f"{mesh!r} and was refused — they are not connected, and {who}, so "
        f"this is yours to decide.{repeat}\n"
        f"grant: connect them (MCP 'connect' with mesh={mesh!r}, "
        f"a={req.by!r}, b={req.other!r}; or "
        f"`claunch mesh connect {mesh} {req.by} {req.other}`) — the requester "
        f"is told, and from then on they settle it between themselves "
        f"instead of through you.\n"
        f"decline: `claunch mesh wire-requests {mesh} --decline "
        f"{req.by} {req.other} --reason \"<why>\"` — say no once and you are "
        f"not asked again about this pair.\n"
        f"Grant when their work depends on each other's answers — a shared "
        f"file, a contested measurement, one's output being the other's "
        f"input. Decline when you meant them to stay independent, or when "
        f"what is actually needed is your ruling rather than a conversation. "
        f"Leaving it open is the one answer that costs you every later "
        f"round: {req.by} cannot reach {req.other} until you say."
    )


def filed_note(req: WireRequest, mesh: str) -> str:
    """What the refused sender is told, in place of "go ask your parent".

    Three states, three different next moves — and none of them is "relay it
    through somebody", which is the sentence this replaces.
    """
    if req.state == GRANTED:
        return (
            f"a wire request for {req.by} <-> {req.other} was granted by "
            f"{req.decided_by or 'an operator'}; if this send still refuses, "
            "the edge was cut again since."
        )
    if req.state == DECLINED:
        why = f": {req.reason}" if req.reason else ""
        return (
            f"{req.decided_by or 'an operator'} declined a channel between "
            f"{req.by} and {req.other}{why}. That decision stands and asking "
            "again does not re-ask it — take what you needed from "
            f"{req.other} to {req.decided_by or 'whoever spawned you'} as a "
            "question about the work, not as a request to be connected."
        )
    if not req.approver:
        return (
            f"a wire request for {req.by} <-> {req.other} is recorded, but no "
            "session here commands either end, so nobody was asked. An "
            f"operator can open it: `claunch mesh connect {mesh} {req.by} "
            f"{req.other}`."
        )
    return (
        f"a wire request for {req.by} <-> {req.other} is filed with "
        f"{req.approver} (asked {req.count}x) — you do NOT need to message "
        f"anyone about it, and you must not send {req.approver} the content "
        f"meant for {req.other}. Carry on with what you can do without that "
        "answer; you are told when it is decided."
    )


def granted_note(req: WireRequest, mesh: str) -> str:
    """What the requester is told when the edge opens."""
    return (
        f"wire request granted: {req.decided_by or 'an operator'} connected "
        f"you to {req.other} in mesh {mesh!r}. Message it directly now — and "
        f"settle what you disagree about against the code, not by asking "
        f"{req.decided_by or 'anyone'} to carry either side of it."
    )


def declined_note(req: WireRequest, mesh: str) -> str:
    """What the requester is told when the answer is no."""
    why = f" — {req.reason}" if req.reason else ""
    return (
        f"wire request declined: {req.decided_by or 'an operator'} will not "
        f"connect you to {req.other} in mesh {mesh!r}{why}. Do not re-file "
        "it; if you still cannot proceed, that is a question about the work "
        f"for {req.decided_by or 'whoever spawned you'}."
    )


def now() -> float:
    """Wall clock for request timestamps.

    ``time.time`` rather than the monotonic clock the delivery gates use:
    these records are persisted and read back after a restart, and a
    monotonic reading means nothing on the other side of one.
    """
    return time.time()
