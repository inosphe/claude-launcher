"""Report-and-end: a session hands what it did to another session, then ends.

Two verbs share this module because they are one mechanism with two
spellings:

* **quick-fork / merge.** ``A`` is copied into ``B`` (claude's
  ``--resume --fork-session`` through the spawn path, so ``B`` is a child of
  ``A`` holding the same conversation up to now) with a marker block at the
  top of the copy — *forked from here*. ``B`` works. When it is done it
  *merges*: it writes a wrap-up of everything since the marker, the daemon
  types that into ``A``, and ``B`` ends. Merge is only offered to a session
  that IS a quick-fork (``SessionDef.quick_fork_of``), and it goes back to
  that one session.
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
for. An agent may also complete without a request (it decided on its own
that the work is done); a request without a completion is ended by the
operator's ordinary kill, and says so.

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


def fork_name(origin: str, taken) -> str:
    """``<origin>-qf<n>``: the first ``n`` no session already holds."""
    n = 1
    while f"{origin}-qf{n}" in taken:
        n += 1
    return f"{origin}-qf{n}"


# ---- the blocks --------------------------------------------------------- #

def compose_marker(
    *, origin: str, fork: str, marker: str, forked_at: str, cwd: str = "",
    mesh: str = "", workflow: str = "",
) -> str:
    """The block at the top of the copy: where the fork begins, and how it
    ends. Fenced like every other machine-generated block a session gets, so
    the agent reads it as the daemon's and not as the user's.

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
    would be a room it cannot send to.
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
        + " -- a fork cannot be given a checkout of its own, because claude "
        "keeps transcripts per directory. So the two of you edit the same "
        "files with no lock between you, and the git state (branch, index, "
        "stash) is one. Keep to an area the origin is not writing, or settle "
        "the boundary with it through a merge, before you touch a file it "
        "may be holding.",
        "merge: when the work is done, write a wrap-up of everything since "
        f"marker {marker} -- what changed, files and commits, decisions taken, "
        "what is still open -- and hand it in with the MCP 'handoff' tool "
        "({text: ...}) or `claunch quick-fork merge -f <file>`. The daemon "
        f"types it into {origin} and ends this session. The wrap-up is the "
        "only thing of this session that survives, so do not merge before "
        "the work is done, and do not end this session any other way.",
    ]
    if mesh:
        lines.append(
            f"mesh: you were put in mesh {mesh} as {fork} -- the mesh the fork "
            "was asked to join. Its members are live sessions doing real work "
            "and what you send is typed into their terminals, so read the "
            "roster before you send: claunch mesh members " + mesh
        )
    if workflow:
        lines.append(
            f"workflow: you were started on the {workflow} cflow run, scoped "
            f"to {fork}. Call the cflow 'status' tool to pick it up."
        )
    lines.append("---")
    return "\n".join(lines)


def compose_request(*, kind: str, source: str, target: str, marker: str = "") -> str:
    """What is typed into the source when an operator presses merge/hand off:
    finish, write it up, hand it in."""
    if kind == MERGE:
        since = f" since marker {marker}" if marker else " since the fork marker"
        return "\n".join([
            "---",
            "# claunch quick-fork: MERGE requested (machine-generated, not typed by the user)",
            f"target: {target}",
            f"marker: {marker or '?'}",
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
    marker: str = "", forked_at: str = "",
) -> str:
    """What the target receives: a fenced header saying who and from where,
    then the agent's text as it wrote it."""
    if kind == MERGE:
        head = [
            "---",
            f"# claunch quick-fork: merged from {source} (machine-generated, not typed by the user)",
            f"fork: {source}",
            f"marker: {marker or '?'}" + (f" (forked from this conversation at {forked_at})" if forked_at else ""),
            f"note: {source} was a copy of this conversation from that point. "
            "The wrap-up below is everything it did after the marker, in its "
            f"own words. {source} has been ended; what it reports is now "
            "yours to carry on.",
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

    # ---- reading ---------------------------------------------------------- #
    def state(self, name: str) -> Optional[dict]:
        row = self.pending.get(name)
        return dict(row) if row else None

    # ---- resolving -------------------------------------------------------- #
    def resolve(self, manager, source: str, *, to: str = "", kind: str = "") -> dict:
        """Settle ``(kind, target)`` for a request or a completion from
        ``source``, refusing what cannot work with the reason.

        A quick-fork's merge target is fixed to its origin; a plain handoff
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
        if not kind:
            kind = MERGE if origin and (not to or to == origin) else HANDOFF
        if kind not in KINDS:
            raise HandoffError(f"unknown kind {kind!r}: one of {', '.join(KINDS)}")
        if kind == MERGE:
            if not origin:
                raise HandoffError(
                    f"session {source!r} is not a quick-fork — merge goes back "
                    "to the session a quick-fork was copied from, and this one "
                    "was not. Use a handoff (pick the target) instead."
                )
            if to and to != origin:
                raise HandoffError(
                    f"session {source!r} is a quick-fork of {origin!r}; a merge "
                    f"goes back there, not to {to!r}. Use a handoff to send it "
                    "elsewhere."
                )
            target = origin
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
        if dst.exited:
            raise HandoffError(
                f"session {target!r} has exited — a report typed into an exited "
                "terminal reaches nobody. Resume it first, or hand off elsewhere."
            )
        return {"kind": kind, "source": source, "target": target}

    # ---- the request ------------------------------------------------------ #
    async def request(self, manager, source: str, *, to: str = "", kind: str = "") -> dict:
        """Type the instruction block into ``source`` and record the pending
        request. Idempotent on the same target; a different target replaces
        the pending one (and says so in the block)."""
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
        text = compose_request(
            kind=row["kind"], source=source, target=row["target"], marker=marker,
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
        received.
        """
        body = (text or "").strip()
        if not body:
            raise HandoffError("'text' is required: the wrap-up or handoff to deliver")
        row = self.pending.get(source) or {}
        # The pending request fills in what the completion left unsaid — its
        # target only when the completion did not name a different kind: a
        # fork asked to hand off to C that instead merges goes to its origin.
        inherit = not kind or kind == row.get("kind")
        settled = self.resolve(
            manager, source,
            to=to or (row.get("target", "") if inherit else ""),
            kind=kind or row.get("kind", ""),
        )
        marker = row.get("marker") or _marker_of(manager, source)
        block = compose_report(
            kind=settled["kind"], source=source, target=settled["target"],
            text=body, marker=marker, forked_at=_forked_at_of(manager, source),
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
        ended = False
        try:
            manager.kill(source, force=False)
            ended = True
        except Exception as exc:  # the report landed; say the ending did not
            log.warning("handoff: %r reported to %r but could not be ended: %s", source, settled["target"], exc)
        return {
            **settled, "marker": marker, "delivered": True, "ended": ended,
            "completed_at": _utcnow(),
        }

    # ---- withdrawing ------------------------------------------------------ #
    async def cancel(self, manager, source: str) -> dict:
        row = self.pending.pop(source, None)
        if row is None:
            raise HandoffError(f"no merge or handoff is pending on {source!r}")
        try:
            session = manager.get(source)
            if not session.exited:
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
