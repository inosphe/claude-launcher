"""Report-and-end: a session hands what it did to another session, then ends.

Two verbs share this module because they are one mechanism with two
spellings:

* **quick-fork / merge.** ``A`` is copied into ``B`` (Claude's
  ``--resume --fork-session`` or Codex's ``fork <id>`` through the spawn path, so ``B`` is a child of
  ``A`` holding the same conversation up to now) with a marker block at the
  top of the copy — *forked from here*. ``B`` works. When it is done it
  *merges*: it writes a wrap-up of everything since the marker, the daemon
  types that into ``A``, and ``B`` ends. Merge is only offered to a session
  that IS a quick-fork (``SessionDef.quick_fork_of``), and it goes back to
  that one session -- while it is live. A copy can itself be quick-forked
  (``A`` -> ``B`` -> ``C``), so its origin may have ended first: then the
  merge goes to the nearest live session above it (:func:`merge_lineage`),
  and with none live the wrap-up is saved beside the copy's record instead
  of being typed into nobody (:meth:`Handoffs.complete`).
* **handoff.** Two sessions with no relation at all: ``D`` writes a handoff
  for ``C``, the daemon types it into ``C``, and ``D`` ends. Anyone live can
  be the target, so this one needs a picker and lives in the detail panel.

Each has two halves, and the split is the point. A **request** is the
operator's press: the daemon types an instruction block into the *source*
(write the wrap-up, hand it in) and records that one is pending, so the
row can say ``merging…``. The **completion** is the agent's: it hands in
the text (MCP ``handoff``, ``claunch quick-fork merge``, ``claunch
handoff``), and only then does the daemon deliver to the target and end the
source — the report must have landed before the terminal it came from is
gone, or a delivery that failed loses the one thing the session was ended
for. A request without a completion is ended by the operator's ordinary
kill, and says so.

A quick-fork does not complete on its own. Its completion is accepted only
while an operator's request is pending, so a copy ends through the web UI's
merge press and nothing else of the copy's own making -- not a wrap-up it
decided was due, not a parent telling it to finish (s15-qf2 merged itself
the moment its parent asked, claunch-guljk). A request a *session* files
(``actor``) is refused for the same reason: it would let the copy, or its
parent, press the button for the operator. The exception is a fork whose
:attr:`SessionDef.self_merge_allowed` is on -- a ``deep_think`` fork, whose
answer is its merge, or one the operator made with the flag. Any other
session (a plain handoff) still completes whenever it decides the work is
done.

Nothing here reads the transcript. The wrap-up is the agent's, because the
agent is the one that knows what since-the-marker *meant*; the daemon's job
is the marker, the relay and the ending.

State is runtime only (``pending``), like the board's wind-downs: a daemon
restart drops a pending request, and the operator presses again.
"""

from __future__ import annotations

import asyncio
import logging
import secrets
from datetime import datetime, timezone
from typing import Dict, Optional

from .. import store
from . import actors

log = logging.getLogger("claunch.handoff")

MERGE = "merge"
HANDOFF = "handoff"
KINDS = (MERGE, HANDOFF)

#: How long a completion waits for the target to take the report before it
#: gives up and keeps the source alive (``daemon.handoff_deliver_timeout``).
#: Delivery waits for a quiet keyboard on the target, and a person typing
#: there for two minutes is a person who will still want the report — so
#: the answer to a timeout is "try again", never "end the source anyway".
DEFAULT_DELIVER_TIMEOUT = 120.0

#: The one line the marker block is recognised by, in the copy and in every
#: instruction that refers back to it.
MARKER_TITLE = "--- forked from here ---"


class HandoffError(Exception):
    """A request or completion that cannot be carried out, with the reason
    written for whoever pressed or called."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def new_marker() -> str:
    """A short id the fork's marker block carries, so a merge can name the
    exact point rather than 'somewhere above'."""
    return "qf-" + secrets.token_hex(4)


def fork_name(origin: str, taken, *, tag: str = "qf") -> str:
    """``<origin>-<tag><n>``: the first ``n`` no session already holds. The
    tag is ``qf`` for a person's press and ``dt`` for a ``deep_think``
    question, so the rail tells the two apart by name."""
    n = 1
    while f"{origin}-{tag}{n}" in taken:
        n += 1
    return f"{origin}-{tag}{n}"


# ---- the blocks --------------------------------------------------------- #

def compose_marker(
    *, origin: str, fork: str, marker: str, forked_at: str, cwd: str = "",
    mesh: str = "", workflow: str = "",
    role: str = "", origin_role: str = "",
    sibling: bool = False, placed_under: str = "",
    workflow_version: int = 0, origin_workflow: str = "",
    self_merge_allowed: bool = False,
) -> str:
    """The block at the top of the copy: where the fork begins, and how it
    ends. Fenced like every other machine-generated block a session gets, so
    the agent reads it as the daemon's and not as the user's.

    ``role`` is the copy's role in ``mesh``, ``origin_role`` the origin's
    there. The conversation above was held under the origin's role, and the
    copy reads every word of it -- the stance, the duties, "I am the
    leader" -- as its own unless told otherwise. So the role line is written
    whenever a role is known, and when the two differ it says, in as many
    words, that the role above is not this session's.

    ``sibling`` places the copy beside the origin rather than under it:
    ``placed_under`` names the parent it shares with the origin, "" a root.

    The ``checkout`` line is not decoration. A fork cannot be given a worktree
    of its own -- claude keeps transcripts per working directory, so a copy
    started anywhere else resolves no conversation and boots empty, which
    :func:`claude_launcher.spawn.check` refuses outright. The copy therefore
    stands in the SAME checkout as its origin, and two claude sessions editing
    one checkout overwrite each other. That is a property of the fork and
    cannot be configured away, so the only defence is that both agents know
    it -- stated here, in the copy's first block, rather than left to be
    discovered in a lost edit.

    ``mesh`` and ``workflow`` are named only when the fork was given them.
    The copy joins neither by default, and a line about a mesh it is not in
    would be a room it cannot send to. ``workflow_version`` (1, 2, or 0 when
    unknown) picks the tool names the run is driven with, and
    ``origin_workflow`` names the run the origin drives, if any -- see
    :func:`_workflow_lines` for why both are spelled out.

    ``self_merge_allowed`` picks the merge line: a copy that may merge on its
    own is told how; any other is told to wait for the operator's press,
    because the daemon refuses the completion until one is pending.
    """
    lines = [
        "---",
        f"# claunch quick-fork: {MARKER_TITLE} (machine-generated, not typed by the user)",
        f"origin: {origin}",
        f"fork: {fork}",
        f"marker: {marker}",
        f"forked_at: {forked_at}",
        f"note: this session ({fork}) is a COPY of the conversation above, "
        f"taken from {origin} at this point. Everything above this block "
        f"happened in {origin} and stays its own; what happens below is this "
        "session's alone, and nothing typed here reaches the origin by itself.",
        f"checkout: this copy runs in the SAME working directory as {origin}"
        + (f" ({cwd})" if cwd else "")
        + " -- claunch keeps a fork in its origin's checkout (Claude "
        "keeps transcripts per directory). So the two of you edit the same "
        "files with no lock between you, and the git state (branch, index, "
        "stash) is one. Keep to an area the origin is not writing, or settle "
        "the boundary with it through a merge, before you touch a file it "
        "may be holding.",
        _merge_line(origin=origin, marker=marker, allowed=self_merge_allowed),
    ]
    if sibling:
        lines.append(
            "placement: this copy is a SIBLING of "
            f"{origin}, not its child -- "
            + (f"it answers to {placed_under}, {origin}'s own parent"
               if placed_under else
               "a root session with no parent, as the origin is")
            + ". The merge still goes back to "
            f"{origin}."
        )
    if mesh:
        lines.append(
            f"mesh: you were put in mesh {mesh} as {fork} -- the mesh the fork "
            "was asked to join. Its members are live sessions doing real work "
            "and what you send is typed into their terminals, so read the "
            "roster before you send: claunch mesh members " + mesh
        )
    if mesh and role:
        if origin_role and origin_role != role:
            lines.append(
                f"role: CHANGED -- the conversation above was {origin}'s, held "
                f"as {origin_role} in mesh {mesh}. That role, its stance and "
                f"its duties stay with {origin} and are NOT yours. You are "
                f"{role} in mesh {mesh} from this block on: where anything "
                f"above speaks as {origin_role} (instructions, stance, "
                "workflow steps, promises made to members), it does not bind "
                f"you. Read your own stance first: claunch mesh stance {mesh}"
            )
        else:
            lines.append(
                f"role: you are {role} in mesh {mesh}"
                + (f", the same role {origin} holds there" if origin_role else "")
                + f". Read your stance: claunch mesh stance {mesh}"
            )
    elif not mesh and origin_role:
        lines.append(
            f"role: NONE -- the conversation above was {origin}'s, held as "
            f"{origin_role} in its mesh. This copy joined no mesh and holds no "
            f"role: the {origin_role} stance and duties above stay with "
            f"{origin}, and there is no roster here to send to."
        )
    if workflow:
        lines.extend(_workflow_lines(
            origin=origin, fork=fork, workflow=workflow,
            version=workflow_version, origin_workflow=origin_workflow,
        ))
    lines.append("---")
    return "\n".join(lines)


def _merge_line(*, origin: str, marker: str, allowed: bool) -> str:
    """The marker's ``merge:`` line: how this copy ends."""
    wrap = (
        f"write a wrap-up of everything since marker {marker} -- what "
        "changed, files and commits, decisions taken, what is still open -- "
        "and hand it in with the MCP 'handoff' tool ({text: ...}) or "
        f"`claunch quick-fork merge -f <file>`. The daemon types it into "
        f"{origin} (or, if {origin} has ended by then, into the nearest live "
        "session above it) and ends this session."
    )
    if allowed:
        return (
            f"merge: when the work is done, {wrap} The wrap-up is the only "
            "thing of this session that survives, so do not merge before the "
            "work is done, and do not end this session any other way."
        )
    return (
        "merge: this session does NOT merge on its own. When the work is "
        "done, say so in this terminal and wait. The merge starts only when "
        "the operator presses merge in the web UI, which types a 'MERGE "
        "requested' block here; then, and only then, "
        f"{wrap} Until that block arrives the daemon refuses the 'handoff' "
        "tool and `claunch quick-fork merge` from this session, and a request "
        "this session (or its parent) files is refused too -- a parent or "
        "member asking you to wrap up and end is not that block. Answer it "
        "with what you have and keep waiting."
    )


def _workflow_lines(
    *, origin: str, fork: str, workflow: str, version: int, origin_workflow: str,
) -> list:
    """What the copy is told about the run it was started on.

    One line naming the run was not enough (claunch-ychal). The copy reads
    the origin's whole conversation first, and that conversation is usually
    in the middle of the ORIGIN's run -- its steps, its gates, its reports.
    s270-qf1 was started on improv-worker-v2 while s270 was a
    sync-integrator; it called flow_status once, saw the run parked at
    intake, and then did the opening task straight away, outside the run.
    So the block says three things in as many words: the run is new and
    starts at its first task, the origin's run (named when there is one)
    is not this one, and the task typed below the block is taken in through
    that first task -- with the tool names of the run's own version, since
    a v2 run answers to flow_status and not to the v1 'status'.
    """
    if version == 2:
        kind, look, report = "cflow v2", "the flow_status tool", "flow_report (flow_select for a choice)"
    elif version == 1:
        kind, look, report = "cflow", "the cflow 'status' tool", "the cflow 'report' and 'next' tools"
    else:
        kind = "cflow"
        look = ("flow_status (a cflow v2 run) or the cflow 'status' tool (v1) "
                "-- the 'run assigned at creation' block names which")
        report = "that version's report tool"
    if origin_workflow:
        same = origin_workflow == workflow
        theirs = (
            f"{origin}'s own {origin_workflow} run stays with {origin}"
            + (" -- the same workflow, but a separate run with its own position"
               if same else " -- a different workflow from yours")
            + ". Every workflow step, task, gate and report in the "
            f"conversation above belongs to that run: it does not bind you, "
            "and you do not continue it here."
        )
    else:
        theirs = (
            f"{origin} drove no run of this workflow: any workflow step, task "
            "or gate in the conversation above is not this run's and does not "
            "bind you."
        )
    return [
        f"workflow: you were started on a NEW {workflow} run ({kind}), scoped "
        f"to {fork}. It is this session's own run and it starts at its first "
        "task with nothing done.",
        f"origin run: {theirs}",
        f"first: before reading for the task or editing a file, call {look} "
        "and do the task it hands you. A task typed below this block is the "
        "work this run carries: take it in through the run's first task (it "
        "is that task's goal), then do each task the run hands out in turn and "
        f"report it with {report}. Doing the task straight away, outside the "
        "run, leaves the run parked at its first task while the work happens "
        "somewhere it does not record.",
    ]


def compose_request(
    *, kind: str, source: str, target: str, marker: str = "", origin: str = "",
) -> str:
    """What is typed into the source when an operator presses merge/hand off:
    finish, write it up, hand it in. ``origin`` differs from ``target`` when
    the merge was delegated past an origin that has ended."""
    if kind == MERGE:
        since = f" since marker {marker}" if marker else " since the fork marker"
        return "\n".join([
            "---",
            "# claunch quick-fork: MERGE requested (machine-generated, not typed by the user)",
            f"target: {target}",
            f"marker: {marker or '?'}",
            *_delegated_lines(origin=origin, target=target, reader="source"),
            "protocol: finish the step you are on, then write a wrap-up of "
            f"everything done{since} -- what changed, files and commits, "
            "decisions taken, what is still open -- and hand it in: the MCP "
            "'handoff' tool with {text: ...}, or `claunch quick-fork merge "
            f"-f <file>`. The daemon types it into {target} and ends this "
            "session. Nothing else you type here reaches it. An operator can "
            "stop this session without the wrap-up at any time (a second "
            "press), so do not stall.",
            "---",
        ])
    return "\n".join([
        "---",
        "# claunch handoff: HANDOFF requested (machine-generated, not typed by the user)",
        f"target: {target}",
        "protocol: finish the step you are on, then write a handoff for "
        f"{target} -- the state of the work, what is done, what is left, and "
        "where it all is (branch, files, issue ids, commands to pick it up) -- "
        f"and hand it in: the MCP 'handoff' tool with {{to: \"{target}\", "
        f"text: ...}}, or `claunch handoff --to {target} -f <file>`. The "
        f"daemon types it into {target} and ends this session. Nothing else "
        "you type here reaches it. An operator can stop this session without "
        "the handoff at any time (a second press), so do not stall.",
        "---",
    ])


def compose_report(
    *, kind: str, source: str, target: str, text: str,
    marker: str = "", forked_at: str = "", origin: str = "",
) -> str:
    """What the target receives: a fenced header saying who and from where,
    then the agent's text as it wrote it.

    ``origin`` is the session the copy was made from. When it is not
    ``target`` the merge was delegated up past an origin that has ended, and
    the header says so: the conversation the copy started from is then the
    origin's, not the reader's."""
    if kind == MERGE:
        delegated = bool(origin) and origin != target
        head = [
            "---",
            f"# claunch quick-fork: merged from {source} (machine-generated, not typed by the user)",
            f"fork: {source}",
            f"marker: {marker or '?'}" + (
                f" (forked from {origin}'s conversation at {forked_at})" if delegated and forked_at
                else f" (forked from this conversation at {forked_at})" if forked_at else ""
            ),
            *_delegated_lines(origin=origin, target=target, reader="target"),
            (f"note: {source} was a copy of {origin}'s conversation from that "
             "point. The wrap-up below is everything it did after the marker, "
             f"in its own words. {source} has been ended; what it reports is "
             "now yours to carry on.")
            if delegated else
            (f"note: {source} was a copy of this conversation from that point. "
             "The wrap-up below is everything it did after the marker, in its "
             f"own words. {source} has been ended; what it reports is now "
             "yours to carry on."),
            "---",
        ]
    else:
        head = [
            "---",
            f"# claunch handoff: from {source} (machine-generated, not typed by the user)",
            f"from: {source}",
            f"note: {source} handed its work to this session and has been "
            "ended. The handoff follows, in its own words; you are the one "
            "holding that work now.",
            "---",
        ]
    return "\n".join(head) + "\n" + text.strip()


def compose_cancel(*, kind: str, target: str) -> str:
    verb = "merge" if kind == MERGE else "handoff"
    return "\n".join([
        "---",
        f"# claunch {('quick-fork' if kind == MERGE else 'handoff')}: {verb} request withdrawn (machine-generated, not typed by the user)",
        f"target: {target}",
        f"note: the {verb} to {target} was cancelled by an operator. Carry on "
        "with what you were doing; nothing was delivered and this session "
        "is not being ended.",
        "---",
    ])


def _delegated_lines(*, origin: str, target: str, reader: str) -> list:
    """The one line a merge block carries when it does not go to the origin."""
    if not origin or origin == target:
        return []
    if reader == "target":
        return [
            f"delegated: {origin}, the session this copy was forked from, has "
            f"ended -- so the wrap-up comes to you, the nearest live session "
            f"above it.",
        ]
    return [
        f"delegated: your origin {origin} has ended, so the wrap-up goes to "
        f"{target}, the nearest live session above it. Write it for a reader "
        f"who did not hold {origin}'s conversation.",
    ]


def compose_undelivered(*, source: str, origin: str, lineage: list, saved: str) -> str:
    """The answer a merge gets when no session above the copy is live:
    written for the agent that handed the wrap-up in, which is the one that
    has to tell the person at its terminal."""
    checked = ", ".join(f"{name} ({state})" for name, state in lineage) or origin
    return (
        f"no live session to merge into: {origin}, the session {source} was "
        f"forked from, and every session above it have ended (checked: "
        f"{checked}). The wrap-up was NOT delivered and {source} was NOT ended; "
        + (f"it is saved at {saved}. " if saved else "it could not be saved. ")
        + "Tell the user in this terminal, and leave it to them: resume one "
        "of those sessions (claunch respawn <name>) and merge again, hand "
        "the work off to a live session (the 'handoff' tool with 'to'), or "
        "end this session."
    )


def merge_lineage(manager, source: str) -> list:
    """Where a merge from ``source`` can go, nearest first, as
    ``(name, state)`` with state ``live``, ``exited`` or ``gone`` (no record).

    The first entry is the copy's origin (``quick_fork_of``). From each
    session the walk goes to its own origin when it is itself a copy, and
    otherwise to its spawn parent -- so a copy of a copy reaches the session
    the chain began from, and past that the tree above it. A name with no
    record is listed and stepped over: the manager escalates the children of
    a removed record to its parent, so the session's other pointer
    (``parent``) already names the next one that survives.

    A ``deep_think`` copy has exactly one entry: its answer belongs to the
    session that asked, and the deadline sweep ends the copy when that one
    is gone (daemon/deep_think.py) -- nobody above asked the question.
    """
    from .manager import ManagerError  # local: manager imports the api that imports us

    def lookup(name):
        try:
            return manager.get(name)
        except ManagerError:
            return None

    src = lookup(source)
    if src is None or not src.sdef.quick_fork_of:
        return []
    out: list = []
    seen = {source}
    cur = src
    while cur is not None:
        nxt = None
        for cand in (cur.sdef.quick_fork_of, cur.sdef.parent):
            if not cand or cand in seen:
                continue
            seen.add(cand)
            rec = lookup(cand)
            if rec is None:
                out.append((cand, "gone"))
                continue
            out.append((cand, "exited" if rec.exited else "live"))
            nxt = rec
            break
        if src.sdef.deep_think:
            return out[:1]
        cur = nxt
    return out


class NoLiveAncestor(HandoffError):
    """A merge whose origin and every session above it have ended."""

    def __init__(self, source: str, origin: str, lineage: list) -> None:
        self.source = source
        self.origin = origin
        self.lineage = lineage
        checked = ", ".join(f"{n} ({st})" for n, st in lineage) or origin
        super().__init__(
            f"no live session to merge {source!r} into: its origin {origin!r} "
            f"and every session above it have ended (checked: {checked}). "
            "Resume one of them (claunch respawn <name>) and merge again, or "
            "hand off to a live session instead."
        )


# ---- the state ------------------------------------------------------------ #

class Handoffs:
    """The daemon's pending requests, and the two operations on them.

    ``pending`` is keyed by source session name and read by the session list
    (the row's ``handoff`` field) the way ``Board.winddowns`` is: it is what
    turns a kill button into ``merging…``.
    """

    def __init__(self) -> None:
        self.pending: Dict[str, dict] = {}
        self._tasks: set = set()
        #: ``(manager, source, settled, text, marker) -> Optional[str]``: the
        #: first one that answers a string writes the block the target gets,
        #: in place of :func:`compose_report`. A deep-think fork's answer is
        #: a merge whose header has to say which question it answers.
        self.composers: list = []
        #: ``(manager, source, settled, text) -> None``, called once the
        #: report has been delivered and before the source is ended -- the
        #: one moment the source's record and the delivered text are both in
        #: hand. A hook that raises is logged and does not undo the delivery.
        self.completed_hooks: list = []

    # ---- reading ---------------------------------------------------------- #
    def state(self, name: str) -> Optional[dict]:
        row = self.pending.get(name)
        return dict(row) if row else None

    # ---- resolving -------------------------------------------------------- #
    def resolve(self, manager, source: str, *, to: str = "", kind: str = "") -> dict:
        """Settle ``(kind, target)`` for a request or a completion from
        ``source``, refusing what cannot work with the reason.

        A quick-fork's merge goes to its origin, or -- when the origin has
        ended -- to the nearest live session above it (:func:`merge_lineage`);
        with none live it raises :class:`NoLiveAncestor`. A plain handoff
        needs a live target that is not the source. ``kind`` left empty is
        read from the source: a quick-fork merges, anything else hands off.
        """
        from .manager import ManagerError  # local: manager imports the api that imports us

        try:
            src = manager.get(source)
        except ManagerError as exc:
            raise HandoffError(str(exc)) from None
        if src.exited:
            raise HandoffError(f"session {source!r} has exited — nothing to hand off")
        origin = src.sdef.quick_fork_of or ""
        lineage = merge_lineage(manager, source) if origin else []
        above = [name for name, _ in lineage]
        if not kind:
            kind = MERGE if origin and (not to or to in above) else HANDOFF
        if kind not in KINDS:
            raise HandoffError(f"unknown kind {kind!r}: one of {', '.join(KINDS)}")
        if kind == MERGE:
            if not origin:
                raise HandoffError(
                    f"session {source!r} is not a quick-fork — merge goes back "
                    "to the session a quick-fork was copied from, and this one "
                    "was not. Use a handoff (pick the target) instead."
                )
            live = next((name for name, state in lineage if state == "live"), "")
            if not live:
                raise NoLiveAncestor(source, origin, lineage)
            # A named target is accepted only when it is where the merge goes
            # anyway: a pending request settled on the nearest live session
            # above, and its completion names that one back.
            if to and to != live:
                raise HandoffError(
                    f"session {source!r} is a quick-fork of {origin!r}; a merge "
                    f"goes back to {live!r}"
                    + (f" (the nearest live session above {origin!r}, which has ended)"
                       if live != origin else "")
                    + f", not to {to!r}. Use a handoff to send it elsewhere."
                )
            target = live
        else:
            target = (to or "").strip()
            if not target:
                raise HandoffError("'to' is required: the session to hand off to")
        if target == source:
            raise HandoffError(f"session {source!r} cannot hand off to itself")
        try:
            dst = manager.get(target)
        except ManagerError:
            raise HandoffError(
                f"no session named {target!r} to hand off to"
                + (" — the quick-fork's origin is gone; use a handoff to a live session instead" if kind == MERGE else "")
            ) from None
        if dst.exited and not getattr(dst, "held_at", None):
            raise HandoffError(
                f"session {target!r} has exited — a report typed into an exited "
                "terminal reaches nobody. Resume it first, or hand off elsewhere."
            )
        settled = {"kind": kind, "source": source, "target": target}
        if kind == MERGE:
            settled["origin"] = origin
            settled["delegated"] = target != origin
        return settled

    # ---- the request ------------------------------------------------------ #
    async def request(
        self, manager, source: str, *, to: str = "", kind: str = "", actor: str = "",
    ) -> dict:
        """Type the instruction block into ``source`` and record the pending
        request. Idempotent on the same target; a different target replaces
        the pending one (and says so in the block).

        ``actor`` is the session that filed the request ("" for the operator
        and the daemon itself). A session may not file one for a quick-fork
        that cannot merge on its own: that request is the operator's press.
        """
        if actor:
            src = _source_of(manager, source)
            if src is not None and not may_self_merge(src.sdef):
                raise HandoffError(
                    f"session {source!r} is a quick-fork: its merge is requested "
                    "only by the operator from the web UI, not by a session "
                    f"({actor!r} asked). Say in your terminal that the work is "
                    "done and wait for the press."
                )
        settled = self.resolve(manager, source, to=to, kind=kind)
        marker = _marker_of(manager, source)
        row = {
            "kind": settled["kind"],
            "target": settled["target"],
            "requested_at": _utcnow(),
            "marker": marker,
            "delivered": None,
        }
        self.pending[source] = row
        if settled.get("delegated"):
            row["origin"] = settled["origin"]
        text = compose_request(
            kind=row["kind"], source=source, target=row["target"], marker=marker,
            origin=settled.get("origin", ""),
        )
        session = manager.get(source)
        task = asyncio.ensure_future(self._deliver_request(session, source, text))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return dict(row)

    async def _deliver_request(self, session, source: str, text: str) -> None:
        try:
            delivered = bool(await session.deliver(text))
        except Exception as exc:
            log.info("handoff: request block not delivered to %r: %s", source, exc)
            delivered = False
        row = self.pending.get(source)
        if row is not None:
            row["delivered"] = delivered

    # ---- the completion --------------------------------------------------- #
    async def complete(
        self, manager, source: str, *, text: str, to: str = "", kind: str = "",
        timeout: Optional[float] = None,
    ) -> dict:
        """Deliver ``text`` to the target, then end ``source``.

        The order is the contract: the report lands first, and a delivery
        that fails or times out keeps the source alive and says so — the
        caller (an agent, usually the source itself) retries or picks
        another target. Nothing is ended on the strength of a report nobody
        received. A merge with no live session above the copy is the one
        completion that does not raise: there is no target to retry, so the
        wrap-up is saved and the answer says so (:meth:`_undelivered`).
        """
        body = (text or "").strip()
        if not body:
            raise HandoffError("'text' is required: the wrap-up or handoff to deliver")
        src = _source_of(manager, source)
        if src is not None and not may_self_merge(src.sdef) and source not in self.pending:
            raise HandoffError(
                f"session {source!r} is a quick-fork and no merge was requested: "
                "a quick-fork ends only after the operator presses merge in the "
                "web UI, which types a 'MERGE requested' block into it. Nothing "
                "was delivered and the session is kept. Say in your terminal "
                "that the work is done and wait for that block."
            )
        row = self.pending.get(source) or {}
        # The pending request fills in what the completion left unsaid — its
        # target only when the completion did not name a different kind: a
        # fork asked to hand off to C that instead merges goes to its origin.
        inherit = not kind or kind == row.get("kind")
        from_row = not to and inherit and bool(row.get("target"))
        to = to or (row.get("target", "") if inherit else "")
        kind = kind or row.get("kind", "")
        try:
            settled = self.resolve(manager, source, to=to, kind=kind)
        except NoLiveAncestor as exc:
            return self._undelivered(manager, source, body, exc)
        except HandoffError:
            # The request settled a merge on a session that has ended since;
            # the completion re-settles on whatever is nearest and live now.
            # Only a target the request supplied -- one the caller named
            # itself is refused as named.
            if not (from_row and kind == MERGE):
                raise
            try:
                settled = self.resolve(manager, source, kind=MERGE)
            except NoLiveAncestor as exc:
                return self._undelivered(manager, source, body, exc)
        marker = row.get("marker") or _marker_of(manager, source)
        block = None
        for compose in self.composers:
            block = compose(manager, source, settled, body, marker)
            if block:
                break
        if not block:
            block = compose_report(
                kind=settled["kind"], source=source, target=settled["target"],
                text=body, marker=marker, forked_at=_forked_at_of(manager, source),
                origin=settled.get("origin", ""),
            )
        target = manager.get(settled["target"])
        if timeout is None:
            cfg = store.daemon_config()
            timeout = float(cfg.get("handoff_deliver_timeout", DEFAULT_DELIVER_TIMEOUT) or 0)
        try:
            if timeout and timeout > 0:
                delivered = bool(await asyncio.wait_for(target.deliver(block), timeout))
            else:
                delivered = bool(await target.deliver(block))
        except asyncio.TimeoutError:
            raise HandoffError(
                f"session {settled['target']!r} did not take the report within "
                f"{timeout:.0f}s (its keyboard is busy or it is not reading) — "
                f"{source!r} is kept; try again"
            ) from None
        except Exception as exc:
            raise HandoffError(
                f"could not deliver to {settled['target']!r}: {exc} — {source!r} is kept"
            ) from None
        if not delivered:
            raise HandoffError(
                f"session {settled['target']!r} did not take the report — "
                f"{source!r} is kept; try again"
            )
        self.pending.pop(source, None)
        for hook in self.completed_hooks:
            try:
                hook(manager, source, settled, body)
            except Exception:  # the report landed; a record that did not is logged
                log.exception("handoff: completion hook failed for %r", source)
        ended = False
        try:
            # The session asked for this ending itself, by handing off.
            manager.kill(source, force=False,
                         by=actors.make(actors.SELF, reason="handoff"))
            ended = True
        except Exception as exc:  # the report landed; say the ending did not
            log.warning("handoff: %r reported to %r but could not be ended: %s", source, settled["target"], exc)
        return {
            **settled, "marker": marker, "delivered": True, "ended": ended,
            "completed_at": _utcnow(),
        }

    def _undelivered(self, manager, source: str, body: str, exc: "NoLiveAncestor") -> dict:
        """A merge with no live session above the copy: keep the wrap-up
        and the copy, and say so where the person will see it.

        Typing the report into an exited terminal reaches nobody, and ending
        the copy would leave the wrap-up nowhere at all. So the text is
        written beside the copy's record (``<session dir>/merge-undelivered-
        <stamp>.md``), the copy stays live, and the answer -- read by the
        agent that handed it in -- tells it to tell the user. A viewer
        attached to the copy also gets a notice over its terminal.
        """
        stamp = _utcnow()
        saved = ""
        try:
            from . import paths

            folder = paths.session_dir(source)
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / f"merge-undelivered-{stamp.replace(':', '').replace('+0000', 'Z')}.md"
            path.write_text(body + "\n", encoding="utf-8")
            saved = str(path)
        except Exception as err:  # the answer still carries the reason
            log.warning("handoff: could not save %r's undelivered wrap-up: %s", source, err)
        self.pending.pop(source, None)
        try:
            manager.get(source).notify(
                f"merge: no live session above {source} to take the wrap-up "
                f"({exc.origin} and its ancestors have ended)"
                + (f" — saved to {saved}" if saved else ""),
                ttl=30, level="warn",
            )
        except Exception:
            pass
        return {
            "kind": MERGE, "source": source, "target": "", "origin": exc.origin,
            "delivered": False, "ended": False, "undelivered": True,
            "lineage": [{"name": n, "state": st} for n, st in exc.lineage],
            "saved": saved,
            "note": compose_undelivered(
                source=source, origin=exc.origin, lineage=exc.lineage, saved=saved,
            ),
        }

    # ---- withdrawing ------------------------------------------------------ #
    async def cancel(self, manager, source: str) -> dict:
        row = self.pending.pop(source, None)
        if row is None:
            raise HandoffError(f"no merge or handoff is pending on {source!r}")
        try:
            session = manager.get(source)
            if not session.exited or getattr(session, "held_at", None):
                task = asyncio.ensure_future(
                    session.deliver(compose_cancel(kind=row["kind"], target=row["target"]))
                )
                self._tasks.add(task)
                task.add_done_callback(self._tasks.discard)
        except Exception as exc:
            log.info("handoff: cancel notice not delivered to %r: %s", source, exc)
        return {**row, "cancelled": True}

    def forget(self, name: str) -> None:
        """A kill or an exit: whatever was pending on ``name`` is moot."""
        self.pending.pop(name, None)

    async def cancel_all(self) -> None:
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.pending.clear()


def may_self_merge(sdef) -> bool:
    """Whether ``sdef`` may complete (or file) a merge/handoff on its own.

    Every session that is not a quick-fork may; a quick-fork only with
    :attr:`SessionDef.self_merge_allowed` on.
    """
    if not getattr(sdef, "quick_fork_of", None):
        return True
    return bool(getattr(sdef, "self_merge_allowed", False))


def _source_of(manager, source: str):
    """The source session, or ``None`` when there is none (``resolve`` then
    refuses with the reason)."""
    from .manager import ManagerError  # local: manager imports the api that imports us

    try:
        return manager.get(source)
    except ManagerError:
        return None


def _marker_of(manager, source: str) -> str:
    """The marker id the fork was made with, read back off the marker block
    in its recorded task — the record is the only place it is written."""
    try:
        task = manager.get(source).sdef.task or ""
    except Exception:
        return ""
    for line in task.splitlines():
        if line.startswith("marker: "):
            return line[len("marker: "):].strip()
    return ""


def _forked_at_of(manager, source: str) -> str:
    try:
        task = manager.get(source).sdef.task or ""
    except Exception:
        return ""
    for line in task.splitlines():
        if line.startswith("forked_at: "):
            return line[len("forked_at: "):].strip()
    return ""
