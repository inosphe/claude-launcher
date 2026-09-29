"""The daemon's side of the board: sessions and their beads issues.

``claunch beads`` (:mod:`claude_launcher.cli_beads`) is how an *agent* reaches
the board. This module is how the *daemon* does, for the three moments where
the board and a session's life meet and no agent is in a position to act:

1. **Creation.** A session made with a task (``claunch new-session``, the web
   form, an agent's ``spawn``) gets an issue on its repository's board —
   created for it, assigned to it, and named in its opening message as
   ``issue: <id>`` so the agent reads the same record the workflows already
   teach (``claunch beads show <id> --json``). A request that already names an
   issue (``issue: <id>`` in the task or context, or an ``issue`` field) adopts
   that one instead of minting a duplicate.

   What a minted issue SAYS need not be the opening task any more: a request
   may carry ``issue_text``, the creation forms' own box, and then the board
   holds the specification while the terminal holds only the first
   instruction. The three answers are exclusive — text, an existing issue, or
   none — and a request that gives two is refused (:func:`check_request`)
   rather than having one of them quietly dropped.

   Adopting is not the same as *taking*. Two sessions assigned to one issue is
   an ownership conflict nobody notices until both have committed, so the
   daemon decides it here, mechanically, from the board and its own session
   list (:func:`adoption`): an issue with no assignee -- or one whose assignee
   is a session that has already exited -- is taken outright, and one a
   *running* session holds is only JOINED. A joiner is linked to the issue
   (its rail shows it, its opening message names it) but the board's
   ``assignee`` is left exactly where it was, and the two sessions are told
   about each other so they can settle it -- the holder over the mesh they
   share, the joiner in its opening block. Nothing is stolen and nothing is
   silently duplicated.

2. **Ending.** A kill does not go straight to SIGTERM any more. When the
   session is alive and holds active issues, the daemon first types a
   wind-down block into it — the list of its issues and what to do with each
   — and waits for the agent to finish that turn (or for a grace period),
   then terminates. Only then is the work the agent had not written down lost
   for a reason it was told about.

3. **Exit.** Whatever ended a session — the wind-down, a crash, ``/exit`` —
   the board is swept once the process is gone: an issue the session was
   working on (``in_progress``, assigned to it) goes back to ``open`` with a
   comment naming the exit, so the next round's board check finds it ready
   instead of orphaned; the placeholder the daemon itself made and nobody ever
   took up is closed. A daemon *restart* is not an exit and sweeps nothing —
   those sessions come back with the same names and the same work.

4. **Queues.** A session's queue is the board's own answer -- the active
   issues assigned to it, in the order the worker takes them -- and is never
   stored beside the session (:func:`queue_of`). The Queues tab of the Beads
   page draws every session's queue as a swimlane and lets the operator drag
   an issue between lanes; that drag is the one write the dashboard makes
   (:meth:`Board.assign`): ``br update --assignee`` plus a ``QUEUED`` /
   ``UNQUEUED`` comment, never a status change.

The matching rule between a session and issues is deliberately loose and
explained per issue (``via``): the recorded link (the session's ``issue``
field), ``assignee``, ``created_by`` (writes are stamped ``--actor
<session>``), and ``issue: <id>`` references in the session's task. A viewer
wants all of them; the sweep acts only on what the session was assigned.

Everything the board says is read with ``br ... --json``; ``br`` runs as a
subprocess through the same :func:`cli_beads.plan` the CLI uses, so the board
a session's daemon sees is the board its agent sees — one per repository,
reached from any worktree.
"""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shutil
import subprocess
import threading
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .. import (
    beads_db, beads_meta, cli_beads, reports as reports_mod, store, workspaces,
)
from .session import (
    CATEGORY_PAUSED, CATEGORY_RUNNING, STATUS_BUSY, STATUS_IDLE, Session,
    session_category,
)

log = logging.getLogger("claude_launcher.daemon.beads")

#: Issue statuses that mean "somebody still means to do this".
ACTIVE_STATUSES = ("open", "in_ready", "in_progress", "in_review", "blocked")

#: Every status a board issue can hold -- the active ones plus ``closed``.
#: What a status filter coming in over the API is checked against, so a
#: typo answers 400 instead of silently listing nothing.
KNOWN_STATUSES = ACTIVE_STATUSES + ("closed",)

#: The label every issue the daemon mints carries, so the sweep can tell its
#: own placeholder from an issue an agent or a human wrote.
SESSION_LABEL = "session"
#: Marks a follow-up a parent handed to a live child at wrapup (improv-worker
#: generated-issue settlement, claunch-zc5ga): the child is its assignee but
#: not its creator, so only this label lets the child's exit release it.
DELEGATED_LABEL = "delegated"

#: ``issue: <id>`` — how a task or a workflow context names the issue a
#: session is for (the improv workflows' own convention).
ISSUE_REF = re.compile(r"\bissue:\s*([A-Za-z][\w.-]*)")

#: What :func:`adoption` decided about an issue a creation request named, and
#: what :meth:`Board.ensure_issue` reports back as ``mode``. ``MINTED`` is the
#: fourth: nothing was named and an issue was created from the task.
TAKE = "assigned"
JOIN = "joined"
MINTED = "created"

#: The two "no issue" answers the creation forms offer. Both mean the same
#: thing to this module -- nothing is minted, nothing is adopted, nothing is
#: assigned -- and they differ only in what the session is TOLD about it,
#: which is the half a session cannot work out for itself. ``NONE_WAIT`` says
#: the goal is the opening task and there is nothing on the board to go
#: looking for; ``NONE_AUTO`` says the opposite, that picking its own work off
#: the board is what this session was created to do and needs no further
#: permission. One answer was ambiguous between the two and the ambiguity was
#: settled by the session guessing, which is how sessions created with "no
#: issue" ended up searching the board for one.
NONE_WAIT = "none-wait"
NONE_AUTO = "none-auto"

#: What the wire accepts for :data:`NONE_WAIT`. ``False`` is the spelling
#: every caller that predates the split sends (``--no-issue``, the forms'
#: ``body.beads = false``) and ``"none"`` is the creation forms' own radio
#: value, so nothing that already works has to be retyped.
_NONE_ALIASES = {"none", "none-wait", "false", "no", "wait"}

#: The sender a daemon-originated ownership notice speaks as on the mesh --
#: not a member, so it is never mistaken for a peer asking for something.
BOARD_SENDER = "beads"

#: How long a board listing is trusted before it is read again. The web UI
#: polls every 2 s; without this each open rail would fork ``br`` at that rate.
CACHE_TTL = 2.0

#: How old a listing the Queues view may still answer with while a fresh one
#: is read behind it (:meth:`Board.issues_or_stale`). The rail asks for the
#: queues every 5 s, which is past :data:`CACHE_TTL`, so without this nearly
#: every Queues open waited on a ``br`` fork -- 0.3 s alone, 0.85 s in a busy
#: daemon (claunch-fa1xk). A write through this daemon drops the listing
#: (:meth:`Board.invalidate`), so a drag is never answered from before itself.
STALE_TTL = 30.0

#: How long one ``br`` command may run before the daemon stops waiting on it.
#: :meth:`Board.br` holds the board's lock while ``br`` runs, so a ``br`` that
#: never ends held every later read of that board: a ``br show`` left in a
#: process that could not finish terminating stalled every session's meta and
#: beads read for hours, and the page's HTTP fallback timed out at the relay
#: (524) behind it (claunch-9urxl). A normal read is well under 5 s.
BR_DEADLINE = 60.0

#: The unassigned pool the Queues page draws folded (its ``BEADS_Q_CELL_CAP``):
#: past this many cards a folded view answers the count and not the cards.
QUEUES_POOL_CAP = 24

#: The most issues an edge read will ask ``br dep list`` about in one pass.
#: The listing already says which issues have outgoing edges at all
#: (``dependency_count``), so on an ordinary board this loop runs a handful of
#: times or not once -- but ``br`` has no bulk edge dump and the per-board lock
#: serialises these, so a board that wired everything to everything would spend
#: the poll interval forking. Past the cap the hierarchy is drawn from the edges
#: that were read and the rest of the board stays flat, which is the same shape
#: a board with no edges draws; the page is never held up for it.
DEPS_SCAN_LIMIT = 200

#: How much of an issue's description a LISTING carries.
#:
#: The dashboard draws an excerpt of a listed issue and nothing more -- six
#: lines or 400 characters in the rail's hover card (``beadPopExcerpt`` in
#: ``web/static/app.js``), nothing at all in a board row -- and the detail
#: pane reads the full text from ``/api/beads/<id>``. So a listing that
#: serialized every description in full was sending text no reader rendered:
#: measured on this machine's own board (1051 issues), ``/api/beads`` was
#: 3.1 MB and ``/api/beads/queues`` 2.3 MB, both of them mostly description.
#: The cut is on the response only; :meth:`Board.issues` still caches the
#: text whole, because the daemon's own lifecycle reads need it.
PREVIEW_CHARS = 700

#: A wind-down: how long the agent has to react to the block at all before it
#: is treated as not listening, and the ceiling on the whole turn after that.
REACT_WINDOW = 20.0
DEFAULT_GRACE = 120.0
DEFAULT_TITLE_LIMIT = 100

#: The actor a dashboard write is stamped with. Not a session: the Queues
#: page moves an assignment on the operator's behalf, and a comment signed by
#: the session it was moved TO would read as that session claiming the work.
DASHBOARD_ACTOR = "dashboard"

#: The actor an issue filed from a session's detail panel is stamped with --
#: its ``created_by`` (claunch-4g76d). The person at the panel wrote it, so the
#: board must say so: stamped with the session instead, the issue reads as the
#: session's own follow-up (:func:`created_of`), and :func:`sweep_plan` treats
#: it as one when that session exits -- releasing or closing work a person
#: queued. The source label is the workflows' own for a person's direct order.
USER_ACTOR = "user"
USER_DIRECT_LABEL = "user-direct"

#: The issue types the workflows use, and what a create coming in over the
#: API is checked against. ``br`` itself takes any word; the check is here so
#: a typed type does not quietly become a category nothing filters on.
KNOWN_TYPES = ("task", "bug", "epic", "doc", "chore", "feature")

#: The priorities a board issue can hold -- ``br``'s own 0..4.
MIN_PRIORITY, MAX_PRIORITY = 0, 4

#: The label a dashboard-filed issue carries, so the board says where it came
#: from the way the workflows' source labels do (user | leader | found | ...).
OPERATOR_LABEL = "user"

Runner = Callable[[List[str], str], Awaitable[Tuple[int, str, str]]]


def _reads_only(args: Sequence[str]) -> bool:
    """Whether this ``br`` argv only reads the board.

    A write invalidates the cached listing; a read must not, or the dashboard's
    own polling would throw away what it just paid for. ``dep`` is the verb that
    is both -- ``dep list`` is how the hierarchy is read and ``dep add`` is how
    it is written -- so it is settled on the subcommand rather than the verb.
    """
    verb = args[0]
    if verb in ("list", "show", "search"):
        return True
    sub = args[1] if len(args) > 1 else ""
    if verb == "comments":
        return sub != "add"
    if verb == "dep":
        return sub in ("list", "tree", "cycles")
    return False


class BeadsUnavailable(cli_beads.BeadsError):
    """The board cannot be reached for this session — no ``br``, no board."""


def _utcnow() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# pure pieces — matching, composition, the sweep's decisions
# --------------------------------------------------------------------------- #
def issue_refs(*texts: Optional[str]) -> List[str]:
    """Every ``issue: <id>`` named in ``texts``, first mention first."""
    seen: List[str] = []
    for text in texts:
        for m in ISSUE_REF.finditer(text or ""):
            ref = m.group(1).rstrip(".,;:")
            if ref and ref not in seen:
                seen.append(ref)
    return seen


def match(
    issues: Sequence[dict],
    name: str,
    *,
    issue: Optional[str] = None,
    task: Optional[str] = None,
) -> List[dict]:
    """The issues that belong to session ``name``, each saying *why*.

    Returns copies with a ``via`` list — ``link`` (the session record names
    it), ``assignee``, ``created_by``, ``task`` (referenced from the task) —
    ordered: the linked issue first, then active ones, then by recency.
    """
    refs = set(issue_refs(task))
    out: List[dict] = []
    for raw in issues:
        via: List[str] = []
        if issue and raw.get("id") == issue:
            via.append("link")
        if raw.get("assignee") == name:
            via.append("assignee")
        if raw.get("created_by") == name:
            via.append("created_by")
        if raw.get("id") in refs:
            via.append("task")
        if via:
            out.append({**raw, "via": via})

    out.sort(
        key=lambda i: (
            0 if "link" in i["via"] else 1,
            0 if i.get("status") in ACTIVE_STATUSES else 1,
            _status_rank(i.get("status")),
            -_ts(i.get("updated_at")),  # newest first within a bucket
        )
    )
    return out


def _status_rank(status: Optional[str]) -> int:
    order = {
        "in_progress": 0,
        "in_review": 1,
        "blocked": 2,
        "in_ready": 3,
        "open": 4,
    }
    return order.get(status or "", 9)


def _ts(iso: Optional[str]) -> float:
    if not iso:
        return 0.0
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp()
    except ValueError:
        return 0.0


def issue_title(task: str, limit: int = DEFAULT_TITLE_LIMIT) -> str:
    """The first line of a task, trimmed to fit an issue title."""
    for line in (task or "").splitlines():
        line = line.strip().lstrip("#-*> ").strip()
        if line:
            return line if len(line) <= limit else line[: limit - 1] + "…"
    return ""


def queue_of(issues: Sequence[dict], name: str) -> List[dict]:
    """Session ``name``'s queue: the active issues the board assigns to it,
    in the order the worker takes them -- priority ascending, then oldest
    first.

    The queue is not stored anywhere: it IS this reading of the board
    (``assignee == name`` over :data:`ACTIVE_STATUSES`), which is what the
    improv workflows' shared queue rule tells a worker to run as ``claunch
    beads list --assignee <session> ...``. Keeping it a function of the
    listing is what keeps the dashboard and the worker reading one queue.
    Pure; ``blocked`` rides along so a row on the Queues page can show it in
    its own column, but the worker's own read leaves it out.
    """
    mine = [
        i for i in issues
        if i.get("assignee") == name and i.get("status") in ACTIVE_STATUSES
    ]
    mine.sort(key=_queue_rank)
    return [dict(i) for i in mine]


def created_of(issues: Sequence[dict], name: str) -> List[dict]:
    """Session ``name``'s own follow-up work: active issues it created but
    does not hold as assignee.

    ``queue_of`` cannot show these -- they carry someone else's assignee, or
    none at all -- and unassigned is exactly the shape a handoff issue takes
    (see the workflows' wrapup rule for follow-up issues created without an
    assignee). Left off the rail, a session's own follow-ups go untracked the
    moment the session that filed them moves on or exits: the case this
    reads (``created_by == name``, not already counted in the assigned
    queue). Same order as ``queue_of``, for the same reason.
    """
    mine = [
        i for i in issues
        if i.get("created_by") == name
        and i.get("assignee") != name
        and i.get("status") in ACTIVE_STATUSES
    ]
    mine.sort(key=_queue_rank)
    return [dict(i) for i in mine]


def _queue_rank(issue: dict) -> Tuple[int, float, str]:
    try:
        pri = int(issue.get("priority") if issue.get("priority") is not None else 9)
    except (TypeError, ValueError):
        pri = 9
    return (pri, _ts(issue.get("created_at")), str(issue.get("id") or ""))


def queue_summary(queue: Sequence[dict]) -> dict:
    """The row head's numbers for one queue: what is waiting to be taken
    (``open``/``in_ready``), being worked, awaiting a landing (``in_review``)
    and blocked -- plus ``next``, the id the worker's ``queue-next`` step
    would pick."""
    waiting = [i for i in queue if i.get("status") in ("open", "in_ready")]
    return {
        "total": len(queue),
        "waiting": len(waiting),
        "working": sum(1 for i in queue if i.get("status") == "in_progress"),
        "review": sum(1 for i in queue if i.get("status") == "in_review"),
        "blocked": sum(1 for i in queue if i.get("status") == "blocked"),
        "next": waiting[0].get("id") if waiting else None,
    }


def preview_row(row: dict) -> dict:
    """``row`` with its description cut to :data:`PREVIEW_CHARS`.

    Always a copy. The row it is given belongs to the cached listing that
    :meth:`Board.issues` hands to the daemon's own lifecycle work, which
    reads the description whole -- trimming in place would take the text away
    from a reader that needs it and leave no trace of having done so.

    ``description_full`` is ``False`` when something was actually cut, so a
    client can tell a description that ends there from one that continues in
    ``/api/beads/<id>``. A row whose description fits carries no such key and
    is unchanged apart from being a copy.
    """
    text = row.get("description")
    if not isinstance(text, str) or len(text) <= PREVIEW_CHARS:
        return dict(row)
    return {**row, "description": text[:PREVIEW_CHARS], "description_full": False}


def preview_rows(rows: Sequence[dict]) -> List[dict]:
    """:func:`preview_row` over a listing."""
    return [preview_row(r) for r in rows]


def assign_note(issue_id: str, target: str, was: str) -> str:
    """The comment a dashboard assignment leaves on the issue, so the board
    says who moved it and from where -- the same ``QUEUED``/``UNQUEUED``
    markers the workflows' queue rule names, which is what a reader greps
    for."""
    if target:
        tail = f" (was {was})" if was else ""
        return f"QUEUED by {DASHBOARD_ACTOR}: assigned to {target}{tail}"
    return f"UNQUEUED by {DASHBOARD_ACTOR}: taken off {was or 'nobody'}"


class AssignRefused(cli_beads.BeadsError):
    """A dashboard assignment that would move work off a session mid-round."""


def workspace_for(cwd: Optional[str]) -> str:
    """The registered workspace a directory belongs to, or ``""``.

    A worktree deep inside a repository answers with the workspace at its
    root, which is the point: the issue records where a session should be
    created, and creation takes a workspace name, not the checkout a previous
    session happened to stand in.
    """
    if not cwd:
        return ""
    try:
        found = workspaces.owning(str(cwd))
    except Exception:            # an unreadable config is not a reason to fail a mint
        return ""
    return found.name if found else ""


#: The headings a board issue's description is written under, in order.
#: The workflows' intake reads these exact strings and ``br lint`` expects
#: them, so they are a contract with something outside this file rather than
#: a formatting choice. One tuple, because two functions write a description
#: here and a third heading set typed into either of them would produce
#: issues that look right and that the intake step cannot read.
SPEC_HEADINGS = ("## 목표", "## 범위(포함·제외)", "## 완료 증거 기준", "## 출처")

#: What the 완료 증거 기준 section says when nobody has filled it in. The
#: same sentence either way it was filed: the assignee is the one who knows.
SPEC_EVIDENCE = "(the assignee fills this in at intake: test counts, commit hash)"


def render_spec(
    goal: str, *, scope: str, source: str, workspace: str = "",
    evidence: str = SPEC_EVIDENCE,
) -> str:
    """A description in the four sections the workflows read.

    The one place :data:`SPEC_HEADINGS` is turned into text. Its callers
    differ only in the goal, who filed it and what the scope placeholder
    says; writing the headings out at each of them is how two issues filed
    by the same daemon come to carry different spellings of the same
    section, which nothing reports because each write looks correct on its
    own.

    ``workspace`` is recorded as YAML front matter above the sections
    (:mod:`claude_launcher.beads_meta`). An empty one writes no block at
    all, so a description composed where no workspace is known looks exactly
    as it did before that existed.
    """
    goal_h, scope_h, evidence_h, source_h = SPEC_HEADINGS
    body = (
        f"{goal_h}\n{goal.strip()}\n\n"
        f"{scope_h}\n{scope}\n\n"
        f"{evidence_h}\n{evidence}\n\n"
        f"{source_h}\n{source}"
    )
    return beads_meta.render(
        {beads_meta.WORKSPACE: workspace} if workspace else {}, body
    )


def compose_description(
    task: str, *, name: str, parent: Optional[str], text: bool = False,
    workspace: str = "",
) -> str:
    """The description of a daemon-minted issue, in the shape the workflows
    require (목표 / 범위 / 완료 증거 기준 / 출처) so ``br lint`` and the next
    reader find the sections they expect. The goal is that text verbatim; the
    rest is left for the agent's intake to fill.

    ``text`` says the goal came from the creation form's own issue box
    rather than being read off the opening task. Only the 출처 line differs,
    and it has to: the two are no longer the same words, so a later reader
    who wants the wording the operator actually filed must be told which of
    the two they are looking at.

    ``workspace`` is the registered workspace the session is being created in,
    recorded as YAML front matter above the sections (:mod:`claude_launcher.beads_meta`).
    An empty one writes no block at all, so an issue minted where no workspace
    is registered looks exactly as it did before this existed.
    """
    origin = (
        f"session {parent} (spawn)" if parent else "operator (new session)"
    )
    source = (
        f"issue text written when session {name} was created"
        if text
        else f"opening task of session {name}"
    )
    # The workspace is recorded at the mint because this is the one moment it
    # is known for certain: the session is being created in a directory right
    # now. Asked for later, it is a guess about where the work belongs.
    return render_spec(
        task,
        scope=(
            "(registered at session creation by the claunch daemon — the "
            "assignee fills this in at intake)"
        ),
        source=f"{origin}, {_utcnow()}, {source}",
        workspace=workspace,
    )


def compose_board_description(title: str, *, workspace: str = "") -> str:
    """The description a dashboard-filed issue starts with.

    The same four sections :func:`compose_description` writes, through the
    same :func:`render_spec`, but filed by a person at the board rather than
    minted for a session -- so the 출처 line says that and no session is
    named.

    An operator who typed a description of their own never reaches this: it
    is the template for the one who typed only a title, and every section
    below the goal says who fills it in.
    """
    return render_spec(
        title,
        scope=(
            "(filed from the dashboard with a title only -- the assignee "
            "fills this in at intake)"
        ),
        source=f"operator (dashboard board form), {_utcnow()}",
        workspace=workspace,
    )


def compose_user_description(
    title: str, *, name: str, details: str = "", workspace: str = "",
) -> str:
    """The description of an issue a person filed from a session's detail
    panel, assigned to that session on the spot (claunch-4g76d).

    The goal is the title, then whatever the person typed under it -- the
    words the worker's intake reads as the round's goal. The 출처 line names
    the person and the session, because the two are different facts here:
    who asked (``created_by``) and who was told to do it (``assignee``).
    """
    goal = title.strip()
    if details.strip():
        goal = f"{goal}\n\n{details.strip()}"
    return render_spec(
        goal,
        scope=(
            "(filed from the session's detail panel -- the assignee fills "
            "this in at intake)"
        ),
        source=(
            f"user (dashboard detail panel), {_utcnow()}, "
            f"filed for and assigned to session {name}"
        ),
        workspace=workspace,
    )


def compose_filed_notice(
    name: str, issue_id: str, title: str, *, priority: int, primary: bool,
) -> str:
    """The block typed into a session a person just filed an issue for.

    The issue is on the board either way; this is what makes it "right away"
    for a session that would otherwise meet it only at its next board read.
    ``primary`` is whether it became the session's own issue (the session had
    none): then it carries the same ``issue: <id>`` line an opening task does,
    so an intake waiting for a goal reads it exactly as one. Otherwise it is
    one more item on the queue, and a round in progress is not interrupted --
    ``queue-next``/``queue-recheck`` read the same listing named here.
    """
    lines = [
        "---",
        "# claunch: the user filed an issue for you -- machine-generated",
        f"issue: {issue_id} -- {title}".rstrip(),
        f"filed by: {USER_ACTOR} (dashboard detail panel); assignee: {name}; "
        f"status: open; priority: P{priority}",
        f"read: claunch beads show {issue_id} --json -- its description is the goal",
    ]
    if primary:
        lines.append(
            "role: this is now this session's issue. If your cflow run is "
            "waiting for a goal, take it as this round's goal now (intake: "
            "move it to in_progress with the run id)."
        )
    else:
        lines.append(
            "role: it is queued behind your current work. Do not drop the "
            "item you are on: your queue step (`claunch beads list --assignee "
            f"{name} --status open --status in_ready --limit 0 --json`) picks "
            "it up. If your run is idle waiting for a goal, take it now."
        )
    lines += [
        "reply: none -- the board is the record.",
        "---",
    ]
    return "\n".join(lines)


def check_new_issue(body: dict) -> dict:
    """Read a board create request, or refuse it.

    Answers the normalised fields; raises :class:`BoardRequestError` with the
    reason otherwise. Pure, so the rules are testable without a board, and
    the one place they are written -- the route validates nothing of its own.

    What it refuses and why:

    * no title -- an issue with no title is unfindable on a board of a
      thousand, and every listing this dashboard draws is titles.
    * a priority outside 0..4, or one that is not a number. ``br`` takes
      ``P2`` as well as ``2``; both are read here and stored as the number.
    * a type the workflows do not use. ``br`` would take the typo and the
      board would gain a category nothing filters on.
    * a workspace that is not registered. An issue naming a directory nobody
      registered sends its session nowhere, and nothing downstream reports
      it -- the same rule :meth:`Board.set_workspace` enforces.
    """
    title = str(body.get("title") or "").strip()
    if not title:
        raise BoardRequestError("an issue needs a title")

    raw_priority = body.get("priority", 2)
    if isinstance(raw_priority, str):
        raw_priority = raw_priority.strip().lstrip("pP") or "2"
    try:
        priority = int(raw_priority)
    except (TypeError, ValueError):
        raise BoardRequestError(f"priority must be a number, not {body.get('priority')!r}")
    if not MIN_PRIORITY <= priority <= MAX_PRIORITY:
        raise BoardRequestError(
            f"priority must be {MIN_PRIORITY}..{MAX_PRIORITY}, not {priority}"
        )

    issue_type = str(body.get("type") or body.get("issue_type") or "task").strip()
    if issue_type not in KNOWN_TYPES:
        raise BoardRequestError(
            f"unknown type {issue_type!r} (known: {', '.join(KNOWN_TYPES)})"
        )

    raw_labels = body.get("labels") or []
    if isinstance(raw_labels, str):
        raw_labels = raw_labels.split(",")
    labels = [str(l).strip() for l in raw_labels if str(l).strip()]
    if OPERATOR_LABEL not in labels:
        labels.insert(0, OPERATOR_LABEL)
    if any("," in l for l in labels):
        raise BoardRequestError("a label cannot contain a comma")

    workspace = str(body.get("workspace") or "").strip()
    if workspace and workspaces.get(workspace) is None:
        raise BoardRequestError(
            f"no workspace named {workspace!r} -- register it with "
            "'claunch workspace add <dir>' first"
        )

    status = str(body.get("status") or "open").strip()
    if status not in ("open", "in_ready"):
        raise BoardRequestError(
            "a new issue starts open, or in_ready when its spec has been "
            f"reviewed -- not {status!r}"
        )

    return {
        "title": title,
        "description": str(body.get("description") or "").strip(),
        "priority": priority,
        "type": issue_type,
        "labels": labels,
        "assignee": str(body.get("assignee") or "").strip(),
        "workspace": workspace,
        "status": status,
        "parent": str(body.get("parent") or "").strip(),
    }


def none_mode(body: dict) -> Optional[str]:
    """Which "no issue" answer a creation request carries, or ``None``.

    Pure and body-only, and the single place the wire spelling is read: every
    other caller asks this rather than comparing ``body["beads"]`` itself, so
    a request that says "no issue" can never mean one thing to the code that
    skips minting and another to the code that writes the opening block --
    which is the shape the session-guesses-its-own-goal failure had.

    ``False`` and ``"none"`` are :data:`NONE_WAIT`, unchanged from before the
    answer was split in two. Anything else, including ``True`` and a missing
    key, is not a "no issue" answer at all.
    """
    value = body.get("beads")
    if value is False:
        return NONE_WAIT
    if isinstance(value, str):
        key = value.strip().lower()
        if key in _NONE_ALIASES:
            return NONE_WAIT
        if key == NONE_AUTO:
            return NONE_AUTO
    return None


def compose_none_note(mode: str, *, session: str = "") -> str:
    """The block appended to the opening task when the answer was "no issue".

    Nothing used to be appended here, because there was no issue to name --
    and that silence was itself the bug. A session that is told nothing about
    the board cannot tell "the operator declined an issue" from "the mint
    failed" or from "the briefing lost it", so a workflow that asks it to
    check which of those happened (improv-worker's ``issue-check``) has no
    record to check and falls through to the branch that searches the board.
    An answer the operator gave has to reach the session that it is about.

    The two modes say opposite things about the same absence, so each spells
    out the action it forbids as well as the one it allows -- a session told
    only "no issue was created" would still be free to conclude that finding
    one is helpful.
    """
    who = session or "$CLAUNCH_SESSION"
    if mode == NONE_AUTO:
        return (
            "no issue: this session was created with the board answer "
            "\"no issue -- assign yourself\". Nothing was minted for you and "
            "nothing is assigned to you, and that is deliberate: picking your "
            "own work off the board is what you are here for. Read the board "
            "(`claunch beads list --status open --json`), take the issue that "
            "fits (`claunch beads update <id> --assignee " + who + " "
            "--status in_progress`), and say in your first report which one "
            "you took and why. You do NOT need anyone to confirm that choice. "
            "If nothing on the board fits, say so rather than minting an "
            "issue for work nobody asked for."
        )
    return (
        "no issue: this session was created with the board answer "
        "\"no issue -- wait for instructions\". Nothing was minted for you and "
        "nothing is assigned to you, and that is deliberate. Do NOT search the "
        "board for work to adopt and do not mint an issue for yourself. Your "
        "goal is the opening task above; if there is none, stay where you are "
        "and wait for the user to type one."
    )


class BoardRequestError(ValueError):
    """A creation request whose board answer contradicts itself."""


def check_request(body: dict) -> None:
    """Refuse a creation request that asks for two board answers at once.

    The board question has exactly one answer per session, and each of the
    three ways of giving it is a different key: ``issue_text`` writes a new
    one, ``issue`` (or an ``issue: <id>`` inside the task or context) adopts
    one that exists, a "no issue" answer (:func:`none_mode` -- ``beads:
    false`` or ``beads: "none-auto"``) asks for none. Sent together they are
    not a preference to resolve — :meth:`Board.ensure_issue` would take the
    adopt branch and the written text would vanish without a word, which is
    the failure shape this whole area was built to remove. So the request is
    refused with both halves named instead.

    Pure and body-only: every field it reads is one the caller sent, and the
    session definition's own ``task``/``issue`` are built from these same
    keys, so there is no conflict here that the body does not already show.
    Raises :class:`BoardRequestError` (a ``ValueError``); the HTTP layer turns
    that into a 400 before anything is created.
    """
    text = str(body.get("issue_text") or "").strip()
    if not text:
        return
    named = str(body.get("issue") or "").strip()
    if named:
        raise BoardRequestError(
            f"'issue_text' writes a new issue and 'issue' adopts {named!r} — "
            "a request cannot mean both. Send one: drop 'issue_text' to work "
            "the issue you named, or drop 'issue' to have one written from "
            "that text"
        )
    none = none_mode(body)
    if none:
        raise BoardRequestError(
            f"'issue_text' writes a new issue and the board answer "
            f"'beads: {none}' asks for none — a request cannot mean both. "
            "Send one"
        )
    refs = issue_refs(body.get("task"), body.get("context"))
    if refs:
        raise BoardRequestError(
            f"'issue_text' writes a new issue, but the task or context names "
            f"issue {refs[0]!r} ('issue: {refs[0]}'), which would be adopted "
            "instead — a request cannot mean both. Drop 'issue_text', or take "
            "that reference out of the text"
        )


def adoption(
    issue: dict, *, session: str, running: Callable[[str], Optional[bool]]
) -> dict:
    """Whether ``session`` may take ``issue`` as its own, or only joins it.

    Pure, and the whole ownership rule in one place: a creation request that
    names an existing issue must never quietly move it off somebody who is
    still working it, and must never leave a free issue unassigned either.

    ``running(name)`` answers what the daemon knows about a name that appears
    as an assignee: ``True`` a session of that name is running here, ``False``
    it is a session of ours that has exited, ``None`` it is nobody the daemon
    knows (a human, a session on another machine).

    The answer is ``{"mode": TAKE|JOIN, "held_by": str|None, "why": str}``:

    - no assignee at all -> TAKE. This is the "빈 beads" case: assign it.
    - assignee is this session already -> TAKE, and the caller writes nothing.
    - assignee is a session that has EXITED -> TAKE. Its exit sweep already
      returned the issue to ``open``; leaving the dead name on it would make
      every future request join a session that cannot answer.
    - assignee is a RUNNING session -> JOIN. Two assignees is the conflict;
      the daemon refuses to create one and hands the two sessions each
      other's names instead.
    - assignee is a name the daemon does not know -> JOIN, for the same
      reason a human's name is not the daemon's to overwrite.
    """
    holder = str(issue.get("assignee") or "").strip()
    if not holder:
        return {"mode": TAKE, "held_by": None, "why": "unassigned"}
    if holder == session:
        return {"mode": TAKE, "held_by": None, "why": "already yours"}
    state = running(holder)
    if state is False:
        return {"mode": TAKE, "held_by": holder, "why": f"{holder} has exited"}
    if state is None:
        return {
            "mode": JOIN, "held_by": holder,
            "why": f"{holder} is not a session on this daemon",
        }
    return {"mode": JOIN, "held_by": holder, "why": f"{holder} is running"}


def compose_link_note(
    issue: str,
    *,
    mode: str,
    held_by: Optional[str] = None,
    mesh: str = "",
    text: bool = False,
) -> str:
    """The ``issue: <id>`` line appended to a new session's opening task.

    One sentence per thing the agent has to know and cannot find out on its
    own: which record is its own, whether it is the assignee, and — when it
    is not — who to settle that with and how. The read command is spelled out
    because the workflows teach that exact call.

    ``text`` is the fourth such thing, and the newest: the issue was minted
    from text the operator wrote into the creation form, so it says something
    the opening task does not. An agent told "registered from this task" would
    reasonably skip reading a record it believes it has already read — which
    is exactly the half of its instructions it would then be missing.
    """
    read = f"read it with `claunch beads show {issue} --json`"
    if mode == JOIN:
        holder = held_by or "another session"
        settle = (
            f"talk to {holder} on mesh {mesh} "
            f'(`claunch mesh send {mesh} {holder} "..."`)'
            if mesh
            else f"raise it with {holder} through whoever created you"
        )
        return (
            f"issue: {issue} -- the board record you were pointed at. "
            f"{holder} is assigned to it and still running, so you are JOINED "
            f"to it, NOT its assignee: {read}, and do not run `claunch beads "
            f"update {issue} --assignee ...` on it. Settle ownership first -- "
            f"{settle} -- and leave a comment saying what you agreed "
            f"(`claunch beads comments add {issue} \"...\"`)."
        )
    if mode == TAKE:
        return (
            f"issue: {issue} -- your board record, assigned to you; {read} "
            "and keep its status current (claunch beads update/comments)."
        )
    if text:
        return (
            f"issue: {issue} -- your board record. It was written separately "
            f"from this opening task and says MORE than it does: {read} "
            "before you start, and treat that text as the specification. Keep "
            "its status current (claunch beads update/comments)."
        )
    return (
        f"issue: {issue} -- your board record, registered from this task; "
        f"{read} and keep its status current (claunch beads update/comments)."
    )


def compose_join_notice(joiner: str, issue: dict, *, holder: str) -> str:
    """What the daemon tells the session that already holds an issue.

    It reports rather than asks — the holder keeps the assignment either way,
    so there is nothing here that stops if it is ignored — and it says the two
    things the holder cannot see from its own terminal: that a second session
    now exists on its issue, and which of them is going to do the work.
    """
    return (
        f"beads: session {joiner} was just created on {issue.get('id')} "
        f"({issue.get('title') or 'no title'}), which is assigned to you. "
        f"The daemon did NOT move the assignment -- {holder} is still its "
        "assignee. Settle which of you owns it: either hand it over "
        f"(`claunch beads update {issue.get('id')} --assignee {joiner}`) or "
        f"tell {joiner} what slice to take, and record the answer as a "
        f"comment on the issue."
    )


def compose_winddown(name: str, issues: Sequence[dict], grace: float) -> str:
    """The block typed into a session that is about to be ended.

    English, like every other block the daemon types. It says what is about
    to happen, lists the issues the session holds, and asks for the smallest
    set of board writes that keep the work findable: finished work to
    ``in_review`` with its evidence, unfinished work with a ``HANDOFF``
    comment. Closing is not asked for — that stays the leader's — and the
    return to ``open`` is the daemon's own job once the process is gone.
    """
    lines = [
        "---",
        "# claunch: this session is being ended -- machine-generated",
        "An operator or your parent asked to end this session. Before it is "
        "stopped, settle your work on the board so nothing is lost with the "
        "context. Your issues:",
    ]
    for i in issues:
        lines.append(
            f"- {i.get('id')} [{i.get('status')}] {i.get('title') or ''}".rstrip()
        )
    lines += [
        "Do now, in this order, with `claunch beads ...`:",
        "1. Commit (or stash) anything worth keeping; note the branch and "
        "hash in a comment: `claunch beads comments add <id> \"<branch> @ "
        "<hash>, tests N/M\"`.",
        "2. Finished work: `claunch beads update <id> --status in_review`.",
        "3. Unfinished work: `claunch beads comments add <id> \"HANDOFF: what "
        "is done, what is left, where it is\"` and leave the status alone — "
        "the daemon returns it to open when you exit.",
        f"4. Then stop: answer `done` and do nothing else. This session is "
        f"terminated once you go idle, or after {int(grace)}s regardless.",
        "---",
    ]
    return "\n".join(lines)


def sweep_plan(
    issues: Sequence[dict], name: str, *, exit_code: Optional[int]
) -> List[List[str]]:
    """The ``br`` writes an exit calls for — pure, so a test can read them.

    What the session was *assigned* is touched as before: ``in_progress``
    goes back to ``open`` with a comment naming the exit (the leader's own
    orphan rule, done at the moment it becomes true instead of at the next
    board check); the daemon's own placeholder, still ``open`` and never
    taken up, is closed. ``in_ready`` keeps its completed triage, while
    ``in_review`` and ``blocked`` are somebody else's turn; all three are
    left as they are.

    Two more shapes are the session's own *follow-up* issues rather than its
    assignment — things it filed for later and that later never came,
    because it exited first. Both are ``created_by == name`` and still
    ``open``/``in_ready``: one nobody ever picked up (no assignee) gets a
    comment marking it an orphaned follow-up, so the leader's own 3-day
    sweep can find it; one it queued to itself (``assignee == name``, and
    not the daemon's own placeholder — that one already closed above and is
    told apart by the ``session`` label) is released back to the pool, its
    assignee cleared, with a comment of the same kind.

    The last shape is a follow-up a *parent* filed and handed to this session
    at its wrapup (``delegated`` label, claunch-zc5ga): still ``open`` or
    ``in_ready`` means the exiting child never took it up, and nothing else
    would release it -- it was not created here and never went in_progress.
    It is released the same way, and the label comes off with the assignee.
    """
    plan: List[List[str]] = []
    code = "unknown" if exit_code is None else str(exit_code)
    for i in issues:
        iid = str(i.get("id") or "")
        if not iid:
            continue
        status = i.get("status")
        assignee = i.get("assignee")
        created_by = i.get("created_by")
        labels = i.get("labels") or []
        if assignee == name:
            if status == "in_progress":
                plan.append(
                    ["comments", "add", iid,
                     f"SESSION ENDED: {name} exited (code {code}); returned to open "
                     f"by the claunch daemon — reassign or resume"]
                )
                plan.append(["update", iid, "--status", "open"])
            elif (
                status == "open"
                and SESSION_LABEL in labels
                and created_by == name
            ):
                plan.append(
                    ["close", iid, "--reason",
                     f"session {name} ended (code {code}) before taking this up"]
                )
            elif (
                status in ("open", "in_ready")
                and created_by == name
                and SESSION_LABEL not in labels
            ):
                plan.append(
                    ["comments", "add", iid,
                     f"SESSION ENDED: creator {name} exited (code {code}); "
                     f"self-queued follow-up released to the pool "
                     f"(SELF-QUEUE RELEASED)"]
                )
                plan.append(["update", iid, "--assignee", ""])
            elif (
                status in ("open", "in_ready")
                and created_by != name
                and DELEGATED_LABEL in labels
            ):
                plan.append(
                    ["comments", "add", iid,
                     f"SESSION ENDED: delegate {name} exited (code {code}) before "
                     f"taking this up; delegated follow-up released to the pool "
                     f"(DELEGATION RELEASED)"]
                )
                plan.append(
                    ["update", iid, "--assignee", "", "--remove-label", DELEGATED_LABEL]
                )
        elif (
            not assignee
            and created_by == name
            and status in ("open", "in_ready")
        ):
            plan.append(
                ["comments", "add", iid,
                 f"SESSION ENDED: creator {name} exited (code {code}); "
                 f"follow-up left unassigned (ORPHANED FOLLOW-UP)"]
            )
    return plan


# --------------------------------------------------------------------------- #
# the board
# --------------------------------------------------------------------------- #
def _board_root(cwd: str) -> Optional[Path]:
    """The root of the board a directory files on — :func:`cli_beads.resolve`
    reduced to the key this class groups by.

    The daemon keys every cache, lock and view by the root rather than by the
    database, because a root resolves to exactly one database
    (:func:`beads_db.ref_for_root`) while the reverse is not true: two boards
    may deliberately be pointed at one file, which is what
    ``claunch-default`` pinned to a workspace's board is.
    """
    ref = cli_beads.resolve(cwd)
    return ref.root_path if ref is not None else None


async def _run_with_deadline(
    argv: List[str], cwd: str, deadline: float
) -> Tuple[int, str, str]:
    """Fork ``argv`` off the event loop thread and wait at most ``deadline``.

    Off the loop thread because on Windows the Proactor loop's transport calls
    Popen -- pipe setup and CreateProcess -- on the loop thread itself. py-spy
    put 106 of 1167 loop samples there (40s, live daemon, claunch-y9ax9),
    every one a stall of every HTTP request and terminal socket.

    Not ``subprocess.run(timeout=)``: after it kills the child it calls
    ``communicate()`` again with no limit, and the ``br`` this guards against
    had been killed and still never finished terminating (claunch-9urxl).
    On the deadline the child is killed and left behind, and so is the
    thread waiting on it -- a daemon thread, so it does not hold the
    process's exit either.
    """
    loop = asyncio.get_running_loop()
    done = loop.create_future()
    started: Dict[str, subprocess.Popen] = {}

    def settle(result, exc) -> None:
        if done.done():
            return
        if exc is not None:
            done.set_exception(exc)
        else:
            done.set_result(result)

    def work() -> None:
        result, error = None, None
        try:
            proc = subprocess.Popen(
                argv, cwd=cwd, stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
            started["proc"] = proc
            out, err = proc.communicate()
            result = (proc.returncode, out, err)
        except BaseException as exc:  # noqa: BLE001 -- handed to the awaiting caller
            error = exc
        try:
            loop.call_soon_threadsafe(settle, result, error)
        except RuntimeError:  # the loop closed while br ran
            pass

    threading.Thread(target=work, name="br", daemon=True).start()
    try:
        code, out, err = await asyncio.wait_for(asyncio.shield(done), deadline)
    except asyncio.TimeoutError:
        proc = started.get("proc")
        if proc is not None:
            log.warning(
                "beads: br pid %s ran past %.0fs, killed and no longer waited on: %s",
                proc.pid, deadline, " ".join(argv[3:])[:120],
            )
            try:
                proc.kill()
            except OSError:
                pass
        raise
    return (
        code or 0,
        out.decode("utf-8", "replace"),
        err.decode("utf-8", "replace"),
    )


class Board:
    """One daemon's access to every repository board its sessions live in.

    ``runner`` runs a ``br`` argv in a directory and answers ``(code, stdout,
    stderr)``; the default forks the real binary, tests hand in a fake.
    ``root_for`` resolves a session directory to the repository that owns the
    board (git's common dir, so worktrees share one) and is likewise
    injectable.
    """

    def __init__(
        self,
        runner: Optional[Runner] = None,
        *,
        root_for: Optional[Callable[[str], Optional[Path]]] = None,
        clock: Callable[[], float] = time.monotonic,
        deadline: float = BR_DEADLINE,
    ) -> None:
        self._runner = runner
        self._deadline = deadline
        self._root_for = root_for or _board_root
        self._clock = clock
        self._roots: Dict[str, Optional[Path]] = {}
        #: Board roots resolved to the database under them. Short-lived
        #: because the mapping is a setting the operator edits while the
        #: daemon runs; the settings route also drops it outright
        #: (:meth:`forget_paths`) so a change is visible on the next poll
        #: rather than after the TTL.
        self._refs: Dict[str, Tuple[float, beads_db.BoardRef]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._cache: Dict[str, Tuple[float, List[dict]]] = {}
        #: Short-lived pages for the board's incremental reader.  This is
        #: separate from ``_cache`` because a full listing is useful to the
        #: daemon's lifecycle work, while the dashboard must not ask a large
        #: board to serialize every issue just to paint its first viewport.
        self._page_cache: Dict[tuple, Tuple[float, List[dict], bool, Optional[int]]] = {}
        #: The dependency edges of a board, cached beside its listing and
        #: dropped with it -- an edge read is derived from the listing it was
        #: taken against, so keeping one past the other would draw a hierarchy
        #: out of issues that are no longer there.
        self._deps: Dict[str, Tuple[float, List[dict]]] = {}
        #: Dependency edges for one streamed page.  They must not share the
        #: full-list cache above: the first 48 issues are not the dependency
        #: graph for every later page.
        self._page_deps: Dict[tuple, Tuple[float, List[dict]]] = {}
        #: Sessions mid wind-down, by name: what was typed and when.
        self.winddowns: Dict[str, dict] = {}
        self._tasks: set = set()
        #: Boards with a background re-read in flight (:meth:`issues_or_stale`).
        self._refreshing: set = set()
        #: Called with the root after every ``br`` write that succeeded —
        #: create, update, close, comments add, dep add, all of them go
        #: through :meth:`br`. The search index's producer hangs here
        #: (:meth:`daemon.rag.RagService.on_board_write`).
        self.write_hooks: List[Callable[[Path], None]] = []
        #: Called, with nothing, after a sweep stamped ``swept_at`` on at
        #: least one session. The daemon hangs the manager's ``persist`` here,
        #: so the stamp reaches the registry now rather than at whatever
        #: persist happens to come next -- a restart before that one would
        #: sweep the same ending again (claunch-fh8u1.2).
        self.swept_hooks: List[Callable[[], None]] = []
        #: ``br`` was found on PATH (sticky), and when PATH was last walked.
        self._which_found = False
        self._which_checked = 0.0
        #: Per board directory, how its ``policy.yaml`` looked when the custom
        #: statuses were last made sure of (:meth:`_ensure_policy`).
        self._policy_seen: Dict[str, tuple] = {}

    # ---- availability -------------------------------------------------- #
    #: How long a "``br`` is not on PATH" answer is reused before PATH is
    #: walked again, so installing it mid-run is noticed without a restart.
    WHICH_MISS_TTL = 30.0

    def available(self) -> bool:
        """Whether ``br`` can be run at all (a fake runner counts).

        Cached, because this is asked on every board view, every session view
        and every session list, and the detail panel polls one of those every
        five seconds per open card. ``shutil.which`` stats each PATH entry
        once per ``PATHEXT`` suffix on Windows, so one answer is dozens of
        filesystem calls on the event loop. A hit is kept for the life of the
        daemon; a miss for :data:`WHICH_MISS_TTL` seconds.
        """
        if self._runner is not None:
            return True
        if self._which_found:
            return True
        if time.monotonic() - self._which_checked < self.WHICH_MISS_TTL:
            return False
        self._which_checked = time.monotonic()
        self._which_found = shutil.which(cli_beads.BINARY) is not None
        return self._which_found

    async def root_for(self, cwd: str) -> Optional[Path]:
        """The repository root owning ``cwd``'s board, or ``None``."""
        if not cwd:
            return None
        if cwd not in self._roots:
            self._roots[cwd] = await asyncio.to_thread(self._root_for, cwd)
        return self._roots[cwd]

    async def _resolve_roots(self, cwds: Sequence[str]) -> None:
        """Warm :meth:`root_for` for every directory in ``cwds`` concurrently.

        Deduplicated first: two sessions in one tree must not each spawn their
        own ``git rev-parse`` for the answer they share.
        """
        wanted = [c for c in dict.fromkeys(cwds) if c and c not in self._roots]
        if not wanted:
            return
        await asyncio.gather(
            *(self.root_for(c) for c in wanted), return_exceptions=True
        )

    def ref_for(self, root: Optional[Path]) -> Optional[beads_db.BoardRef]:
        """Which database ``root``'s board is, and what that board is called.

        Cached for :data:`CACHE_TTL` because every listing, every view and
        every ``br`` call asks it, and answering reads the config file.
        """
        if root is None:
            return None
        key = str(root)
        now = self._clock()
        hit = self._refs.get(key)
        if hit is not None and now - hit[0] < CACHE_TTL:
            return hit[1]
        ref = beads_db.ref_for_root(root) or beads_db.plain_ref(root)
        self._refs[key] = (now, ref)
        return ref

    def forget_paths(self) -> None:
        """Drop every resolved directory, board and listing.

        What a change to the board settings needs done for it: the directory
        a session sits in may now resolve to a different database, so an
        answer taken against the old one is not stale, it is the wrong
        board's.
        """
        self._roots.clear()
        self._refs.clear()
        self._cache.clear()
        self._page_cache.clear()
        self._deps.clear()
        self._page_deps.clear()

    def has_board(self, root: Optional[Path]) -> bool:
        """Whether ``root`` has a board, counting one that does not exist yet.

        A registered workspace with no ``.beads/`` at all still answers yes:
        its board is created on first use (:func:`cli_beads.autocreatable`),
        and saying no here is what used to leave every such workspace's
        sessions reporting "no board" while their issues went to the daemon's
        own directory instead.
        """
        if root is None:
            return False
        ref = self.ref_for(root)
        if ref is None:
            return False
        if ref.exists():
            return True
        if (Path(ref.root) / cli_beads.BEADS_DIR / cli_beads.JSONL_NAME).is_file():
            return True
        return cli_beads.autocreatable(ref)

    # ---- running br ----------------------------------------------------- #
    async def _run(self, argv: List[str], cwd: str) -> Tuple[int, str, str]:
        """Run ``argv`` and answer ``(code, stdout, stderr)``, or raise
        :class:`cli_beads.BeadsError` once it has run past the deadline --
        so the caller's board lock is released (claunch-9urxl)."""
        try:
            if self._runner is not None:
                return await asyncio.wait_for(self._runner(argv, cwd), self._deadline)
            return await _run_with_deadline(argv, cwd, self._deadline)
        except asyncio.TimeoutError:
            raise cli_beads.BeadsError(
                f"br {' '.join(argv[3:])[:80]} did not finish in "
                f"{self._deadline:.0f}s"
            ) from None

    async def create_board(self, ref: beads_db.BoardRef) -> None:
        """Bring ``ref``'s database into being — the daemon's side of
        :func:`cli_beads.create_board`.

        The plan is the CLI's, so a board made here and a board made by
        ``claunch beads`` in a session are laid out identically; only the
        running of it differs, because this one must not block the event
        loop. Called with the board's lock already held.
        """
        if not self.available():
            raise BeadsUnavailable(
                f"'{cli_beads.BINARY}' is not installed on the daemon machine"
            )

        def runner(argv: List[str], cwd: str):
            # The subprocess itself is async; this closure only has to look
            # synchronous to cli_beads.create_board, which is run in a
            # thread. asyncio.run_coroutine_threadsafe hands the call back
            # to the loop that owns the runner.
            future = asyncio.run_coroutine_threadsafe(self._run(argv, cwd), loop)
            return future.result()

        loop = asyncio.get_running_loop()
        await asyncio.to_thread(cli_beads.create_board, ref, runner)
        self._refs.pop(str(ref.root), None)

    async def init_board(self, ref: beads_db.BoardRef) -> dict:
        """Set ``ref``'s board up in full -- the Settings page's button, and
        the daemon's side of :func:`cli_beads.init_board` (what ``claunch
        beads init --workspace`` runs), whose answer this returns.

        Taken under the board's lock, so it cannot race the first ``br``
        call that would create the same database on its own.
        """
        if not self.available():
            raise BeadsUnavailable(
                f"'{cli_beads.BINARY}' is not installed on the daemon machine"
            )
        loop = asyncio.get_running_loop()

        def runner(argv: List[str], cwd: str):
            # As in create_board: the async runner, called from the thread.
            future = asyncio.run_coroutine_threadsafe(self._run(argv, cwd), loop)
            return future.result()

        root = Path(ref.root)
        lock = self._locks.setdefault(str(root), asyncio.Lock())
        async with lock:
            result = await asyncio.to_thread(cli_beads.init_board, ref, runner)
        self._refs.pop(str(ref.root), None)
        self._policy_seen.pop(str(root / cli_beads.BEADS_DIR), None)
        self.invalidate(root)
        return result

    def _ensure_policy(self, beads_dir: Path) -> None:
        """Make sure ``beads_dir``'s policy declares the custom statuses.

        :func:`cli_beads.ensure_policy`, looked at again only when the file or
        the directory changed since the last look -- every ``br`` call comes
        through here and the dashboard makes one every two seconds. A write
        that fails is logged, not raised: the call it sits in front of still
        runs, and a status filter it breaks answers with ``br``'s own words.
        """
        target = beads_dir / cli_beads.POLICY_NAME

        def stamp() -> tuple:
            try:
                return (beads_dir.is_dir(), target.stat().st_mtime_ns)
            except OSError:
                return (beads_dir.is_dir(), None)

        key = str(beads_dir)
        now = stamp()
        if self._policy_seen.get(key) == now:
            return
        try:
            cli_beads.ensure_policy(beads_dir)
        except OSError as exc:
            log.warning("beads: could not declare statuses in %s: %s", target, exc)
        self._policy_seen[key] = stamp()

    async def br(
        self, root: Path, args: List[str], *, actor: Optional[str] = None,
        ref: Optional[beads_db.BoardRef] = None,
    ):
        """Run one ``br`` command against ``root``'s board; parsed JSON back.

        Writes invalidate the listing cache for that board. A non-zero exit
        is a :class:`cli_beads.BeadsError` carrying ``br``'s own words.
        ``ref`` names the board outright instead of resolving it from
        ``root`` -- for a caller holding a row that already says which
        database it means (the Settings page's boards card).
        """
        if not self.available():
            raise BeadsUnavailable(
                f"'{cli_beads.BINARY}' is not installed on the daemon machine"
            )
        ref = ref or self.ref_for(root)
        assert ref is not None  # root is not None here; ref_for only nulls on that
        beads_dir = Path(ref.root) / cli_beads.BEADS_DIR
        key = str(root)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            # Both of these are read under the lock, not before it: creating
            # the board is a step of its own, and two callers that each read
            # "missing" outside the lock would both run it.
            if (
                not ref.exists()
                and not (beads_dir / cli_beads.JSONL_NAME).is_file()
                and cli_beads.autocreatable(ref)
                and args[:1] != ["init"]
            ):
                await self.create_board(ref)
            self._ensure_policy(beads_dir)
            commands = cli_beads.plan(
                list(args) + (["--json"] if "--json" not in args else []),
                Path(ref.root),
                actor,
                db_exists=ref.exists(),
                jsonl_exists=(beads_dir / cli_beads.JSONL_NAME).is_file(),
                db=ref.db,
            )
            out = ""
            for cmd in commands:
                code, out, err = await self._run(cmd, str(root))
                if code != 0:
                    detail = (err or out).strip()
                    raise cli_beads.BeadsError(
                        f"br {' '.join(cmd[3:])[:80]} failed ({code}): {detail}"
                    )
        if args and not _reads_only(args):
            self.invalidate(root)
            for hook in list(self.write_hooks):
                try:
                    hook(root)
                except Exception:  # a producer must not fail the write
                    log.exception("beads: write hook %r failed for %s", hook, root)
        text = out.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except ValueError:
            # br prints a human line before the JSON on some writes
            start = min(
                (p for p in (text.find("{"), text.find("[")) if p >= 0),
                default=-1,
            )
            if start < 0:
                return None
            try:
                return json.loads(text[start:])
            except ValueError:
                return None

    def invalidate(self, root: Path) -> None:
        """Drop every cached reading of ``root``'s board: the listing, its
        pages and the dependency edges taken against them. What :meth:`br`
        does after a write, and what a write the daemon did not make
        (``claunch beads …`` runs ``br`` itself) needs done for it."""
        key = str(root)
        self._cache.pop(key, None)
        self._page_cache = {
            page: cached for page, cached in self._page_cache.items()
            if page[0] != key
        }
        self._page_deps = {
            page: cached for page, cached in self._page_deps.items()
            if page[0] != key
        }
        self._deps.pop(key, None)

    async def issues(self, root: Path) -> List[dict]:
        """Every issue on ``root``'s board, closed ones included — cached
        briefly, because the dashboard asks every two seconds."""
        key = str(root)
        hit = self._cache.get(key)
        now = self._clock()
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
        data = await self.br(root, ["list", "--all", "--limit", "0"])
        rows = data.get("issues") if isinstance(data, dict) else data
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        self._cache[key] = (now, rows)
        return rows

    async def issues_or_stale(self, root: Path) -> List[dict]:
        """:meth:`issues`, except that a listing past :data:`CACHE_TTL` but
        within :data:`STALE_TTL` is answered at once while a fresh one is read
        in the background for the next caller. For views polled on a clock
        (the Queues tab, the rail's pills), where one poll of lag costs
        nothing and a ``br`` fork on every open is the wait the page showed
        as "loading…"."""
        key = str(root)
        hit = self._cache.get(key)
        if hit is None or self._clock() - hit[0] >= STALE_TTL:
            return await self.issues(root)
        if self._clock() - hit[0] >= CACHE_TTL and key not in self._refreshing:
            self._refreshing.add(key)

            async def refresh() -> None:
                try:
                    await self.issues(root)
                except Exception as exc:  # the next caller reads it itself
                    log.debug("beads: background listing of %s failed: %s", root, exc)
                finally:
                    self._refreshing.discard(key)

            task = asyncio.ensure_future(refresh())
            self._tasks.add(task)
            task.add_done_callback(self._tasks.discard)
        return hit[1]

    async def issue_page(
        self, root: Path, *, offset: int = 0, limit: int = 50,
        priority: Optional[int] = None,
        sort: str = "updated_at", direction: str = "desc",
        statuses: Optional[Sequence[str]] = None,
        assignee: str = "",
    ) -> Tuple[List[dict], bool, Optional[int]]:
        """One bounded page of a board in the requested order.

        ``br list`` performs the offset, priority and status filtering in the
        board's database.  Asking for one extra row makes the continuation
        marker independent of a separate count query.  The small local slice
        keeps the contract correct for older ``br`` versions (and test
        runners) that ignore pagination flags.

        ``statuses`` is the filter the page is NUMBERED under, which is why
        it belongs here rather than in the client: a reader looking at open
        work wants page 2 of the open issues, and a page filtered after it
        was cut holds however many of its fifty rows happened to be open --
        a different count on every page, and pages that are entirely empty
        while the board still has hundreds of matching issues. ``None`` is
        every status, closed ones included.

        Answers ``(page, has_more, total)``.  ``total`` is how many issues
        match the filter on the whole board, which is what a page control
        needs to say how many pages there are; it is ``None`` when ``br``
        does not report one.

        ``assignee`` is an exact assignment filter applied before pagination;
        a creator or historical session link does not count as assignment.
        """
        offset = max(0, offset)
        limit = max(1, limit)
        if sort not in {"updated_at", "created_at", "priority", "title"}:
            raise ValueError("unsupported sort field")
        if direction not in {"asc", "desc"}:
            raise ValueError("unsupported sort direction")
        wanted = tuple(dict.fromkeys(statuses or ()))
        key = (str(root), offset, limit, priority, sort, direction, wanted, assignee)
        now = self._clock()
        hit = self._page_cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1], hit[2], hit[3]
        args = [
            "list", "--all", "--limit", str(limit + 1), "--offset", str(offset),
            "--sort", sort,
        ]
        # br defaults dates to newest first, priority/title to ascending.
        if (direction == "asc") == (sort in {"updated_at", "created_at"}):
            args.append("--reverse")
        if priority is not None:
            args.extend(["--priority", str(priority)])
        if assignee:
            args.extend(["--assignee", assignee])
        for status in wanted:
            # Repeated, never comma-joined: a comma list is accepted and
            # matches nothing (claunch-beads-list-comma-status-tmh).
            args.extend(["--status", status])
        data = await self.br(root, args)
        rows = data.get("issues") if isinstance(data, dict) else data
        rows = [r for r in (rows or []) if isinstance(r, dict)]
        total = data.get("total") if isinstance(data, dict) else None
        if not isinstance(total, int):
            total = None
        if priority is not None:
            rows = [r for r in rows if r.get("priority") == priority]
        if wanted:
            rows = [r for r in rows if r.get("status") in wanted]
        if assignee:
            rows = [r for r in rows if r.get("assignee") == assignee]
        # A compatible but pre-pagination ``br`` can return the full list.
        # Its response is larger than the requested extra row, which is an
        # unambiguous signal to apply the requested window locally -- and
        # then the rows in hand ARE the whole match, so they are the count.
        if len(rows) > limit + 1:
            total = len(rows)
            rows = rows[offset:offset + limit + 1]
        has_more = len(rows) > limit
        page = rows[:limit]
        self._page_cache[key] = (now, page, has_more, total)
        return page, has_more, total

    async def edges(
        self, root: Path, rows: Sequence[dict], *, cache_key: Optional[tuple] = None,
    ) -> List[dict]:
        """``root``'s dependency edges, as ``{from, to, type}`` — child first.

        ``br`` has no bulk edge dump: ``list`` reports only how many an issue
        has, and the graph command it does have covers open work alone, which
        would drop a closed child out from under an open parent exactly where
        the page wants to show it. So the edges are read one issue at a time --
        but only for the issues the listing already says have any. Each edge is
        stored on the depending side, so asking every issue with a non-zero
        ``dependency_count`` sees every edge exactly once and asking the other
        side would only see them twice.

        Direction is ``br``'s own: ``br dep add <child> <parent> --type
        parent-child`` makes the CHILD the depending issue, so ``from`` is the
        child and ``to`` is the parent (measured against ``br epic status``,
        which counts the depended-on issue as the one with children).

        An issue whose edges cannot be read is skipped rather than failing the
        board: a hierarchy is a nicety over a listing that is already useful,
        and a page that showed nothing because one edge read broke would be
        trading the whole board for the ornament.
        """
        key = str(root) if cache_key is None else cache_key
        now = self._clock()
        cache = self._deps if cache_key is None else self._page_deps
        hit = cache.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
        # Read through br, never out of the database file: br 0.7's engine
        # (frankensqlite) keeps its own WAL index, and a SQLite reader that
        # opened the file made br's next call set that index aside as
        # poisoned and leave a .br-wal-index-*/ directory behind -- one per
        # read, and this read ran on every Beads page load. One fork per
        # issue with edges is slower; making it fast again without opening
        # the file is its own piece of work.
        wanted = [
            r.get("id") for r in rows
            if r.get("id") and (r.get("dependency_count") or 0)
        ]
        if len(wanted) > DEPS_SCAN_LIMIT:
            log.info(
                "beads: %s has %d issues with dependencies, reading the first "
                "%d", key, len(wanted), DEPS_SCAN_LIMIT,
            )
            wanted = wanted[:DEPS_SCAN_LIMIT]
        out: List[dict] = []
        for issue_id in wanted:
            try:
                data = await self.br(root, ["dep", "list", issue_id])
            except cli_beads.BeadsError as exc:
                log.debug("beads: no edges for %r: %s", issue_id, exc)
                continue
            if isinstance(data, dict):
                data = data.get("dependencies") or data.get("edges") or []
            for row in data or []:
                if not isinstance(row, dict):
                    continue
                # `dep list` answers in two shapes across br versions: the
                # stored edge (issue_id/depends_on_id) and the resolved target
                # (the depended-on issue itself, under `id`). Both name the
                # same edge; the asked-for issue is the depending side either
                # way, which is what makes the second shape readable at all.
                target = row.get("depends_on_id") or row.get("id")
                if not target:
                    continue
                out.append({
                    "from": row.get("issue_id") or issue_id,
                    "to": target,
                    "type": row.get("type") or row.get("dependency_type") or "",
                })
        cache[key] = (now, out)
        return out

    async def show(self, root: Path, issue_id: str) -> dict:
        """One issue in full, with its comments."""
        data = await self.br(root, ["show", issue_id])
        issue = data[0] if isinstance(data, list) and data else data
        if not isinstance(issue, dict):
            raise cli_beads.BeadsError(f"no issue {issue_id!r}")
        try:
            comments = await self.br(root, ["comments", "list", issue_id])
        except cli_beads.BeadsError:
            comments = []
        if isinstance(comments, dict):
            comments = comments.get("comments") or []
        return {**issue, "comments": comments if isinstance(comments, list) else []}

    # ---- per-session views ---------------------------------------------- #
    async def session_view(self, session) -> dict:
        """What the board says about one session — for the rail."""
        sdef = session.sdef
        view = {
            "available": self.available(),
            "root": None,
            # Which board this session's directory files on, so the rail can
            # say it by name. Filled beside "root" below; null until then.
            "board": None,
            "db": None,
            "workspace": None,
            "issue": sdef.issue,
            "issues": [],
            # The round reports this session has left on disk, newest first.
            # A file index in the board view looks like a category error until
            # you ask what a reader of this panel wants: the issue says what
            # the round was FOR, and the report says what came of it. Reading
            # the directory is the whole index (the filenames carry the time
            # and the issue), so this costs one listdir and needs no registry
            # to keep in sync with the files. It is filled before any of the
            # early returns below, because a report outlives the board -- a
            # machine with no 'br' installed still has its reports, and a
            # panel that hid them because the board was unavailable would be
            # hiding the one thing it could still show.
            "reports": reports_mod.listing(sdef.name),
            "error": None,
        }
        if not view["available"]:
            view["error"] = f"'{cli_beads.BINARY}' is not installed on the daemon machine"
            return view
        try:
            root = await self.root_for(sdef.cwd)
        except Exception as exc:  # git missing, odd path
            view["error"] = str(exc)
            return view
        if not self.has_board(root):
            view["error"] = (
                "no board: this directory is in no registered workspace "
                "and in no checkout that already holds one — register it "
                "with 'claunch workspace add <dir>', or in Settings ▸ "
                "Workspaces"
            )
            return view
        view["root"] = str(root)
        head = self.board_head(root)
        view["board"] = head["board"]
        view["db"] = head["db"]
        view["workspace"] = head["workspace"]
        try:
            rows = await self.issues(root)
        except cli_beads.BeadsError as exc:
            view["error"] = str(exc)
            return view
        view["issues"] = preview_rows(
            match(rows, sdef.name, issue=sdef.issue, task=sdef.task)
        )
        wd = self.winddowns.get(sdef.name)
        if wd:
            view["winddown"] = wd
        return view

    def board_head(self, root: Path) -> dict:
        """The fields every board entry opens with: which board this is.

        ``root`` alone used to be the whole identity, and the page printed it
        as the board's title -- a directory path, the same one for every
        workspace once the fleet had settled on the daemon's own board. The
        name is what the operator set the board up as (a workspace name, or
        ``claunch-default``), ``db`` is the file it actually reads, and
        ``configured`` says whether that file was chosen or derived. The page
        shows the name and keeps the path for the tooltip.
        """
        ref = self.ref_for(root)
        if ref is None:
            return {"root": str(root), "board": Path(root).name, "db": "",
                    "workspace": "", "configured": False, "board_exists": False}
        return {
            "root": str(root),
            "board": ref.name,
            "db": ref.db,
            "workspace": ref.workspace,
            "configured": ref.configured,
            "board_exists": ref.exists(),
        }

    async def _group_by_root(
        self, sessions: Sequence, extra_roots: Sequence[str] = ()
    ) -> Tuple[Dict[str, List], List[Path]]:
        """The fleet by board: ``{root: [sessions]}`` and the roots in first-seen
        order (``extra_roots`` last, sessionless). Shared by the two fleet-wide
        views so they cannot disagree about which board a session is on."""
        await self._resolve_roots(
            [s.sdef.cwd for s in sessions] + list(extra_roots)
        )
        by_root: Dict[str, List] = {}
        order: List[Path] = []
        for s in sessions:
            try:
                root = await self.root_for(s.sdef.cwd)
            except Exception:
                root = None
            if not self.has_board(root):
                continue
            key = str(root)
            if key not in by_root:
                by_root[key] = []
                order.append(root)
            by_root[key].append(s)
        for cwd in extra_roots:
            try:
                root = await self.root_for(cwd)
            except Exception:
                root = None
            if self.has_board(root) and str(root) not in by_root:
                by_root[str(root)] = []
                order.append(root)
        return by_root, order

    async def fleet_view(self, sessions: Sequence, extra_roots: Sequence[str] = ()) -> dict:
        """Every board the fleet touches, each issue tagged with the sessions
        it belongs to — for the Beads page."""
        result = {"available": self.available(), "boards": []}
        if not result["available"]:
            result["error"] = f"'{cli_beads.BINARY}' is not installed on the daemon machine"
            return result
        # Resolve every directory's board first, all at once. root_for shells
        # out to `git rev-parse` for a cwd it has not seen, and the fleet is
        # spread over one worktree per session -- awaited one at a time that
        # was seventeen sequential process spawns before the page could draw
        # anything, which is most of the ~9s this endpoint cost on a daemon
        # that had not been asked yet. They are independent questions, so they
        # are asked together; the answers are memoised, so this is a one-time
        # cost per directory either way and every later poll skips it.
        by_root, order = await self._group_by_root(sessions, extra_roots)
        for root in order:
            entry: dict = {
                **self.board_head(root),
                "issues": [], "deps": [], "sessions": [], "error": None,
            }
            members = by_root[str(root)]
            entry["sessions"] = [
                {"name": s.sdef.name, "status": s.status(), "issue": s.sdef.issue}
                for s in members
            ]
            try:
                rows = await self.issues(root)
            except cli_beads.BeadsError as exc:
                entry["error"] = str(exc)
                result["boards"].append(entry)
                continue
            owners: Dict[str, List[dict]] = {}
            for s in members:
                for m in match(rows, s.sdef.name, issue=s.sdef.issue, task=s.sdef.task):
                    owners.setdefault(m["id"], []).append(
                        {"name": s.sdef.name, "via": m["via"], "status": s.status()}
                    )
            for raw in rows:
                entry["issues"].append(
                    {**preview_row(raw), "sessions": owners.get(raw.get("id"), [])}
                )
            # The edges the page nests the board by. Read after the issues and
            # from them, so a board that could not be listed never reaches here
            # -- there is nothing to hang a hierarchy on.
            try:
                entry["deps"] = await self.edges(root, rows)
            except cli_beads.BeadsError as exc:
                log.debug("beads: no edge read for %s: %s", root, exc)
                entry["deps"] = []
            result["boards"].append(entry)
        return result

    async def boards_view(
        self, sessions: Sequence, extra_roots: Sequence[str] = (),
    ) -> dict:
        """Which boards exist and who is on them -- no issues.

        What a form needs before it can be drawn: where an issue may be
        filed, and which session names an assignee picker may offer. It is
        deliberately the cheapest reading on this class -- it resolves
        directories to boards and stops -- because a create form that had to
        wait for a board listing would be paying the cost a paged board was
        built to avoid.
        """
        by_root, order = await self._group_by_root(sessions, extra_roots)
        return {
            "available": self.available(),
            "boards": [
                {
                    **self.board_head(root),
                    "sessions": [
                        {"name": s.sdef.name, "status": s.status()}
                        for s in by_root[str(root)]
                    ],
                }
                for root in order
            ],
        }

    async def stream_view(
        self, sessions: Sequence, extra_roots: Sequence[str] = (), *,
        offset: int = 0, limit: int = 50, priority: Optional[int] = None,
        sort: str = "updated_at", direction: str = "desc",
        statuses: Optional[Sequence[str]] = None,
        assignee: str = "",
        board_root: Optional[str] = None,
    ) -> dict:
        """One bounded page of each board on the Beads screen.

        The cursor is an offset shared by the boards in this response.  A
        fleet normally has one board; with several boards, the client asks
        for the same window of each.  Existing :meth:`fleet_view` remains the
        complete compatibility response used by callers that need every issue
        at once.

        ``statuses`` narrows the page in the board's database rather than
        after it was cut, so the page the reader is on is a page OF what is
        being shown.  Each board entry carries ``total`` -- how many issues
        match on that board -- so a page control can say how many pages there
        are instead of only whether one more exists.

        Descriptions are cut to :data:`PREVIEW_CHARS` (see
        :func:`preview_row`): a listing draws an excerpt, and the full text
        is one request away at ``/api/beads/<id>``.

        ``board_root`` selects which board's issues to read while keeping all
        board headers for workspace navigation. ``None`` preserves the legacy
        all-board response; an empty or unknown root selects the first board.
        """
        result = {
            "available": self.available(), "boards": [], "offset": offset,
            "limit": limit, "priority": priority, "has_more": False,
            "next_offset": None,
            "statuses": list(statuses or ()),
            "total": None,
        }
        if not result["available"]:
            result["error"] = f"'{cli_beads.BINARY}' is not installed on the daemon machine"
            return result
        by_root, order = await self._group_by_root(sessions, extra_roots)
        # New clients select a workspace before paging. Retain every header
        # for workspace tabs, but only read issues from the selected board.
        # An empty or removed selection falls back to the first known board.
        selected = None
        if board_root is not None and order:
            selected = next((root for root in order if str(root) == board_root), order[0])
        for root in order:
            entry: dict = {
                **self.board_head(root),
                "issues": [], "deps": [], "sessions": [], "error": None,
                "has_more": False, "total": None,
            }
            members = by_root[str(root)]
            entry["sessions"] = [
                {"name": s.sdef.name, "status": s.status(), "issue": s.sdef.issue}
                for s in members
            ]
            if selected is not None and root != selected:
                result["boards"].append(entry)
                continue
            try:
                rows, entry["has_more"], entry["total"] = await self.issue_page(
                    root, offset=offset, limit=limit, priority=priority,
                    sort=sort, direction=direction, statuses=statuses, assignee=assignee,
                )
            except cli_beads.BeadsError as exc:
                entry["error"] = str(exc)
                result["boards"].append(entry)
                continue
            owners: Dict[str, List[dict]] = {}
            for s in members:
                for m in match(rows, s.sdef.name, issue=s.sdef.issue, task=s.sdef.task):
                    owners.setdefault(m["id"], []).append(
                        {"name": s.sdef.name, "via": m["via"], "status": s.status()}
                    )
            entry["issues"] = [
                {**preview_row(raw), "sessions": owners.get(raw.get("id"), [])}
                for raw in rows
            ]
            try:
                entry["deps"] = await self.edges(
                    root, rows,
                    cache_key=(
                        str(root), offset, limit, priority, sort, direction,
                        tuple(statuses or ()), assignee,
                    ),
                )
            except cli_beads.BeadsError as exc:
                log.debug("beads: no edge read for %s: %s", root, exc)
            result["has_more"] = result["has_more"] or entry["has_more"]
            if entry["total"] is not None:
                result["total"] = (result["total"] or 0) + entry["total"]
            result["boards"].append(entry)
        if result["has_more"]:
            result["next_offset"] = offset + limit
        return result

    async def queues_view(
        self,
        sessions: Sequence,
        extra_roots: Sequence[str] = (),
        *,
        cflow_for: Optional[Callable[[str, str], Optional[dict]]] = None,
        activity_for: Optional[Callable[[Session], dict]] = None,
        fold: bool = False,
        open_folds: Sequence[str] = (),
    ) -> dict:
        """Every board's queues -- the Queues tab (and the session rail's
        pills): one lane per session (and per assignee the daemon does not
        know), each carrying the issues the board assigns to it in the order
        the worker takes them (``issues``), the active issues it created but
        does not hold as assignee (``created`` -- its own follow-ups, most at
        risk of going untracked once it moves on or exits), plus the
        unassigned pool the operator drags from.

        Each lane also carries the session's ``category`` -- running, paused,
        killed or archived (:func:`session.session_category`, the partition
        the session list filters on; ``None`` for an assignee the daemon does
        not know). The page draws the running and paused lanes and folds the
        rest: ``status`` cannot tell them apart, since a paused session has
        exited and answers ``exited`` like a killed one.

        The lanes are sessions first, in the daemon's order, then any other
        assignee an active issue names (a human, a session on another
        machine) -- a card that could not be dragged back to a lane the page
        does not draw would be stuck. An exited session with nothing assigned
        and no follow-up of its own draws no lane -- unless it is paused,
        since a paused session is one the operator means to resume and so a
        row to drag work onto; one that still holds either does, so what it
        left behind can be seen and moved.
        ``cflow_for(name, cwd)`` is the run summary a lane head shows beside
        the session's status (``None`` for none).

        ``activity_for(session)`` is what the lane head's status dot is graded
        by -- the readings the rail's own dot reads (``moved_rows``,
        ``tool_calls``, ``last_activity_at``, ``paused_at``), merged into the
        lane. The page prefers the rail's fresher record of the same session
        and falls back to these for one the rail's filter left out
        (claunch-t76lb).

        ``fold`` answers the parts the page draws folded as counts: a lane
        the page folds (not running or paused) carries ``folded: true`` and
        ``count`` with empty ``issues``/``created``, and a pool of more than
        :data:`QUEUES_POOL_CAP` carries ``unassigned_count`` with an empty
        ``unassigned``. On this machine's board those cards were 1.1 MB of a
        1.35 MB answer, for rows nobody had opened (claunch-fa1xk).
        ``open_folds`` names the folds the reader has opened -- ``"spent"``,
        ``"pool"`` -- which come in full. Without ``fold`` (the rail's pills,
        which draw killed sessions' issues too) everything comes in full.
        The listing may be up to :data:`STALE_TTL` old
        (:meth:`issues_or_stale`).
        """
        opened = set(open_folds)
        result = {
            "available": self.available(),
            "statuses": list(ACTIVE_STATUSES),
            "boards": [],
        }
        if not result["available"]:
            result["error"] = f"'{cli_beads.BINARY}' is not installed on the daemon machine"
            return result
        by_root, order = await self._group_by_root(sessions, extra_roots)
        for root in order:
            entry: dict = {
                **self.board_head(root),
                "lanes": [], "unassigned": [], "error": None,
            }
            members = by_root[str(root)]
            try:
                rows = await self.issues_or_stale(root)
            except cli_beads.BeadsError as exc:
                entry["error"] = str(exc)
                result["boards"].append(entry)
                continue
            active = [r for r in rows if r.get("status") in ACTIVE_STATUSES]
            names: List[str] = []
            for s in members:
                if s.sdef.name not in names:
                    names.append(s.sdef.name)
            others = sorted({
                str(r.get("assignee")) for r in active
                if r.get("assignee") and str(r.get("assignee")) not in names
            })
            by_name = {s.sdef.name: s for s in members}
            for name in names + others:
                s = by_name.get(name)
                queue = queue_of(active, name)
                created = created_of(active, name)
                category = session_category(s) if s is not None else None
                if (s is not None and s.status() == "exited"
                        and category != CATEGORY_PAUSED
                        and not queue and not created):
                    continue
                lane: dict = {
                    "session": name,
                    "known": s is not None,
                    "status": s.status() if s is not None else None,
                    "category": category,
                    "issue": s.sdef.issue if s is not None else None,
                    "cflow": None,
                    # The cards carry an excerpt, not the text: every lane on
                    # this page repeats its issues' descriptions, which made
                    # the response 2.3 MB on this machine's own board.
                    "issues": preview_rows(queue),
                    "created": preview_rows(created),
                    "summary": queue_summary(queue),
                }
                if s is not None and cflow_for is not None:
                    try:
                        lane["cflow"] = cflow_for(name, s.sdef.cwd or "")
                    except Exception as exc:  # a run state that cannot be read
                        log.debug("beads: no cflow summary for %r: %s", name, exc)
                # The page's own fold rule (app.js beadsLaneSpent).
                if (fold and "spent" not in opened
                        and category not in (CATEGORY_RUNNING, CATEGORY_PAUSED)):
                    lane.update(folded=True, count=len(queue), issues=[], created=[])
                elif s is not None and activity_for is not None:
                    # Only for a head that is drawn: a folded lane has none.
                    try:
                        lane.update(activity_for(s))
                    except Exception as exc:  # a dot is not worth the page
                        log.debug("beads: no activity for %r: %s", name, exc)
                entry["lanes"].append(lane)
            pool = [r for r in active if not r.get("assignee")]
            pool.sort(key=_queue_rank)
            if fold and "pool" not in opened and len(pool) > QUEUES_POOL_CAP:
                entry["unassigned"] = []
                entry["unassigned_count"] = len(pool)
            else:
                entry["unassigned"] = preview_rows(pool)
            result["boards"].append(entry)
        return result

    async def assign(
        self,
        root: Path,
        issue_id: str,
        session: Optional[str],
        *,
        manager=None,
        force: bool = False,
    ) -> dict:
        """The Queues page's one write: move ``issue_id`` onto ``session``'s
        queue (or off every queue, with ``None``/``""``).

        Exactly ``br update <id> --assignee <session>`` plus a ``QUEUED``/
        ``UNQUEUED`` comment -- what the leader types by hand today. The status
        is never touched: taking an issue up (``in_progress``), asking for a
        landing (``in_review``) and closing stay the assignee's, as the
        workflows' shared block says.

        One refusal, the same rule :func:`adoption` applies at creation: an
        issue that is ``in_progress`` under a session that is RUNNING is not
        moved. That session has a branch with the work on it, and an
        assignment that walked away from it would leave the commit with no
        issue to close against. ``force`` overrides it for the operator who
        knows better; the refusal names the holder so they can decide.
        """
        target = str(session or "").strip()
        self._cache.pop(str(root), None)
        rows = await self.issues(root)
        current = next((r for r in rows if r.get("id") == issue_id), None)
        if current is None:
            raise cli_beads.BeadsError(f"no issue {issue_id!r} on {root}")
        was = str(current.get("assignee") or "").strip()
        if was == target:
            return {"issue": issue_id, "assignee": target, "was": was, "changed": False}
        if (
            was
            and current.get("status") == "in_progress"
            and self._running(manager)(was) is True
            and not force
        ):
            raise AssignRefused(
                f"{issue_id} is in_progress under {was}, which is still running "
                f"-- its branch carries that work. Let {was} finish or hand it "
                "over on the board, or send force to move it anyway"
            )
        await self.br(
            root, ["update", issue_id, "--assignee", target], actor=DASHBOARD_ACTOR
        )
        await self.br(
            root, ["comments", "add", issue_id, assign_note(issue_id, target, was)],
            actor=DASHBOARD_ACTOR,
        )
        return {"issue": issue_id, "assignee": target, "was": was, "changed": True}

    # ---- metadata ------------------------------------------------------- #
    async def set_workspace(
        self, root: Path, issue_id: str, name: Optional[str]
    ) -> dict:
        """Record (or clear) the workspace an issue's session should be created
        in, as YAML front matter on its description.

        The dashboard's second write, and it is a write to the DESCRIPTION
        rather than to a field of its own, because the board has no field of
        its own to give: ``br`` stores what it stores, and a fact this tool
        needs back exactly has to live in the text. :mod:`claude_launcher.beads_meta`
        keeps it separable from the prose, so an issue whose workspace is
        recorded still reads as the spec it was.

        ``name`` must be a REGISTERED workspace (:func:`workspaces.get`) or
        empty to clear it. An unregistered name is refused rather than stored:
        an issue naming a directory nobody registered would send its session
        nowhere, and the failure would surface as a session created in the
        wrong tree — which nothing downstream reports.
        """
        target = str(name or "").strip()
        if target and workspaces.get(target) is None:
            raise cli_beads.BeadsError(
                f"no workspace named {target!r} -- register it with "
                "'claunch workspace add <dir>' first"
            )
        issue = await self.show(root, issue_id)
        was = beads_meta.workspace_of(issue)
        if was == target:
            return {
                "issue": issue_id, "workspace": target, "was": was, "changed": False,
            }
        before = issue.get("description") or ""
        updated = beads_meta.set_key(before, beads_meta.WORKSPACE, target)
        args = ["update", issue_id, "--description", updated]
        # br 0.7 refuses to shorten a description below half its length
        # without --force (a guard against an agent wiping a spec). Clearing
        # the front matter off a short spec trips it, and what goes is only
        # the key this method owns -- beads_meta keeps the prose.
        if len(updated) < len(before):
            args.append("--force")
        self._cache.pop(str(root), None)
        await self.br(root, args, actor=DASHBOARD_ACTOR)
        return {"issue": issue_id, "workspace": target, "was": was, "changed": True}

    # ---- creation ------------------------------------------------------- #
    @staticmethod
    def _running(manager) -> Callable[[str], Optional[bool]]:
        """``adoption``'s view of a name: running here / exited here / unknown.

        A manager is optional so the board stays usable without one (tests,
        and any caller that has no session list); with none, every assignee
        reads as unknown, which is the conservative answer -- JOIN rather than
        take something that may still be somebody's.
        """
        def running(name: str) -> Optional[bool]:
            if manager is None:
                return None
            try:
                other = manager.get(name)
            except Exception:
                return None
            return not other.exited

        return running

    async def ensure_issue(
        self, session, *, body: dict, parent: Optional[str], manager=None
    ) -> Optional[dict]:
        """Give a new session its issue: adopt the one the request names, or
        mint one from its task. ``None`` when there is nothing to do -- no task
        and no reference, no board, ``br`` missing, or the feature is off
        (``beads_auto_issue``, or a "no issue" answer on the request --
        :func:`none_mode` -- which is what the creation forms send for both
        shapes of "no issue at all").

        Adopting goes through :func:`adoption`, so which of the two things it
        means is decided from the board rather than assumed: an unheld issue
        is *taken* (``--assignee`` written), one a running session holds is
        only *joined* and the board's assignment is not touched. ``manager`` is
        what makes that distinction possible -- the daemon's session list, used
        to tell a live holder from a dead name.

        A minted issue is written from ``issue_text`` when the request carries
        one -- the creation forms' own box for what the work IS, as opposed to
        the opening task, which is what the session is TOLD. They started as
        the same words and no longer have to be: an operator who wants the
        board to hold the specification and the terminal to hold the first
        instruction writes both. With the box empty it falls back to the task,
        which is what every caller that predates the field still gets.

        Never raises: a board that cannot be written must not cost a session
        its launch. Returns ``{"issue": id, "created": bool, "mode": ...,
        "held_by": name|None, "from_issue_text": bool, "why": str}`` on
        success, where ``mode`` is one of :data:`MINTED`, :data:`TAKE`,
        :data:`JOIN` (``from_issue_text`` only on a mint).
        """
        cfg = store.daemon_config()
        if not cfg.get("beads_auto_issue", True) or none_mode(body):
            return None
        sdef = session.sdef
        task = str(body.get("task") or sdef.task or "")
        context = str(body.get("context") or "")
        written = str(body.get("issue_text") or "").strip()
        explicit = str(body.get("issue") or sdef.issue or "").strip() or None
        refs = ([explicit] if explicit else []) + [
            r for r in issue_refs(task, context) if r != explicit
        ]
        # Written text is a reason to mint on its own: "no task" no longer
        # means "nothing to write down" now that the two are separate boxes,
        # and a session created with only an issue text must still get its
        # issue.
        if not refs and not task.strip() and not written:
            return None
        if not self.available():
            return None
        try:
            root = await self.root_for(sdef.cwd)
        except Exception:
            return None
        if not self.has_board(root):
            return None
        name = sdef.name
        try:
            if refs:
                iid = refs[0]
                rows = await self.issues(root)
                current = next((r for r in rows if r.get("id") == iid), None)
                if current is None:
                    log.info("session %r names issue %r that is not on the board", name, iid)
                    return None
                verdict = adoption(
                    current, session=name, running=self._running(manager)
                )
                if verdict["mode"] == TAKE:
                    if current.get("assignee") != name:
                        await self.br(
                            root, ["update", iid, "--assignee", name], actor=name
                        )
                else:
                    # A joiner writes no assignment, but the issue must still
                    # say a second session is on it -- the holder may never
                    # read its terminal, and this comment is what a later
                    # reader of the board sees instead of two silent owners.
                    await self.br(
                        root,
                        ["comments", "add", iid,
                         f"JOINED: session {name} was created on this issue "
                         f"while {verdict['held_by']} holds it; assignee left "
                         "unchanged by the claunch daemon -- settle ownership "
                         "and record it here"],
                        actor=name,
                    )
                return {
                    "issue": iid, "created": False, "mode": verdict["mode"],
                    "held_by": verdict["held_by"], "why": verdict["why"],
                    "issue_row": current,
                }
            # The written text wins over the task when both are there: it is
            # the more deliberate of the two, and the only reason to fill the
            # box at all is that the task's wording is not what belongs on the
            # board.
            goal = written or task
            title = issue_title(goal) or f"session {name}"
            label = "leader" if parent else "user"
            data = await self.br(
                root,
                [
                    "create", title,
                    "--type", "task",
                    "--priority", "2",
                    "--labels", f"{SESSION_LABEL},{label}",
                    "--assignee", name,
                    "--description",
                    compose_description(
                        goal, name=name, parent=parent, text=bool(written),
                        workspace=workspace_for(getattr(session.sdef, "cwd", "")),
                    ),
                ],
                actor=name,
            )
            iid = None
            if isinstance(data, dict):
                iid = data.get("id") or (data.get("issue") or {}).get("id")
            elif isinstance(data, list) and data and isinstance(data[0], dict):
                iid = data[0].get("id")
            if not iid:
                return None
            return {
                "issue": str(iid), "created": True, "mode": MINTED,
                "held_by": None, "from_issue_text": bool(written),
                "why": "minted from the issue text" if written
                       else "minted from the opening task",
            }
        except cli_beads.BeadsError as exc:
            log.warning("beads: could not register an issue for %r: %s", name, exc)
            return None

    async def candidates(self, cwd: str, manager=None) -> dict:
        """The issues a creation form may offer for ``cwd``'s board.

        Every issue somebody still means to do, most-urgent first, each
        carrying the answer :func:`adoption` would give if it were picked --
        ``mode`` (would a new session take it, or only join it) and ``held_by``
        -- so the form can *say* "s129 holds this" beside the row instead of
        the user finding out after the session exists. The verdict is computed
        for a session that does not exist yet, so it is asked under a name no
        session has; the only branch that would differ for the real one is the
        "already yours" shortcut, which a new session never hits.
        """
        view = {"available": self.available(), "root": None, "board": None,
                "db": None, "issues": [], "error": None}
        if not view["available"]:
            view["error"] = (
                f"'{cli_beads.BINARY}' is not installed on the daemon machine"
            )
            return view
        try:
            root = await self.root_for(cwd)
        except Exception as exc:  # git missing, odd path
            view["error"] = str(exc)
            return view
        if not self.has_board(root):
            view["error"] = (
                "no board: this directory is in no registered workspace "
                "and in no checkout that already holds one — register it "
                "with 'claunch workspace add <dir>', or in Settings ▸ "
                "Workspaces"
            )
            return view
        view["root"] = str(root)
        head = self.board_head(root)
        view["board"] = head["board"]
        view["db"] = head["db"]
        try:
            rows = await self.issues(root)
        except cli_beads.BeadsError as exc:
            view["error"] = str(exc)
            return view
        running = self._running(manager)
        active_rows = [r for r in rows if r.get("status") in ACTIVE_STATUSES]
        active_rows.sort(
            key=lambda r: (
                _status_rank(r.get("status")),
                int(r.get("priority") or 9),
                -_ts(r.get("updated_at")),
            )
        )
        for raw in active_rows:
            verdict = adoption(raw, session="", running=running)
            view["issues"].append({
                "id": raw.get("id"),
                "title": raw.get("title") or "",
                "status": raw.get("status"),
                "priority": raw.get("priority"),
                "issue_type": raw.get("issue_type"),
                "assignee": raw.get("assignee") or "",
                "mode": verdict["mode"],
                "held_by": verdict["held_by"],
                "why": verdict["why"],
            })
        return view

    async def create_for(
        self, session, *, title: str, description: str = "", priority: int = 2,
    ) -> dict:
        """The detail panel's inline create: an issue a person files for a
        session and assigns to it in the same write (claunch-4g76d).

        Stamped :data:`USER_ACTOR`, never the session: the person filed it,
        and ``created_by == <session>`` would make it the session's own
        follow-up -- drawn as such on the rail, and released or closed by
        :func:`sweep_plan` when the session exits. For the same reason it
        carries no :data:`SESSION_LABEL`, which marks the daemon's own
        placeholder. Status stays ``open`` with the session as assignee: that
        is exactly the listing the worker's queue steps read, and taking it
        up (``in_progress``) remains the assignee's own transition.

        ``description`` is what the person typed under the title; it becomes
        part of the goal section rather than replacing the four-section spec
        the intake reads.
        """
        root = await self.root_for(session.sdef.cwd)
        if not self.has_board(root):
            raise BeadsUnavailable("no board for this session's directory")
        name = session.sdef.name
        desc = compose_user_description(
            title, name=name, details=description,
            workspace=workspace_for(session.sdef.cwd),
        )
        # The assignee last, as every create here writes it: a board that
        # failed halfway should not leave work assigned with no goal in it.
        data = await self.br(
            root,
            [
                "create", title, "--type", "task", "--priority", str(priority),
                "--labels", USER_DIRECT_LABEL, "--description", desc,
                "--assignee", name,
            ],
            actor=USER_ACTOR,
        )
        iid = data.get("id") if isinstance(data, dict) else None
        if not iid and isinstance(data, list) and data:
            iid = data[0].get("id")
        if not iid:
            raise cli_beads.BeadsError("br create answered without an id")
        iid = str(iid)
        # The same marker the Queues tab's drag leaves, so a reader grepping
        # the board for how work reached a queue finds this path too.
        try:
            await self.br(
                root,
                ["comments", "add", iid,
                 f"QUEUED by {USER_ACTOR}: filed from the detail panel and "
                 f"assigned to {name}"],
                actor=USER_ACTOR,
            )
        except cli_beads.BeadsError as exc:
            log.warning("beads: QUEUED comment on %s failed: %s", iid, exc)
        return {"issue": iid, "created": True, "assignee": name,
                "priority": priority, "root": str(root)}

    async def create_issue(self, root: Path, spec: dict) -> dict:
        """File an issue on ``root``'s board from the dashboard's form.

        ``spec`` is what :func:`check_new_issue` answered -- this method does
        no validating of its own, so there is one place the rules live.

        The write is stamped :data:`DASHBOARD_ACTOR` rather than a session:
        an operator filed it, and a ``created_by`` naming a session would put
        the issue on that session's rail as its own follow-up work (see
        :func:`created_of`), which is a different fact.

        The workspace is recorded the way :meth:`set_workspace` records it --
        front matter on the description -- so an issue filed here is
        immediately one the "Start a session" block can open in the right
        directory. It is written in the SAME create as the description
        rather than as a second update, so an issue never exists with its
        directory missing.
        """
        if not self.has_board(root):
            raise BeadsUnavailable(f"no board at {root}")
        description = spec["description"] or compose_board_description(
            spec["title"], workspace=spec["workspace"],
        )
        if spec["workspace"]:
            # An operator-written description gets the block put on it; the
            # template above already carries one, and set_key replacing an
            # identical value writes the same bytes back.
            description = beads_meta.set_key(
                description, beads_meta.WORKSPACE, spec["workspace"],
            )
        args = [
            "create", spec["title"],
            "--type", spec["type"],
            "--priority", str(spec["priority"]),
            "--labels", ",".join(spec["labels"]),
            "--description", description,
        ]
        if spec["status"] != "open":
            args.extend(["--status", spec["status"]])
        if spec["parent"]:
            args.extend(["--parent", spec["parent"]])
        # Last, and on its own: the workflows' create spec puts the assignee
        # after the description for the same reason -- a board that failed
        # halfway should not leave an issue assigned to somebody with no
        # goal written in it.
        if spec["assignee"]:
            args.extend(["--assignee", spec["assignee"]])
        data = await self.br(root, args, actor=DASHBOARD_ACTOR)
        iid = data.get("id") if isinstance(data, dict) else None
        if not iid and isinstance(data, list) and data:
            iid = data[0].get("id")
        if not iid:
            raise cli_beads.BeadsError("br create answered without an id")
        return {
            "issue": str(iid),
            "root": str(root),
            "workspace": spec["workspace"],
            "created": True,
        }

    # ---- ending --------------------------------------------------------- #
    async def active_issues(self, session) -> List[dict]:
        """The issues a wind-down should mention: matched, and still open."""
        view = await self.session_view(session)
        return [i for i in view["issues"] if i.get("status") in ACTIVE_STATUSES]

    async def begin_winddown(self, session: Session, manager) -> bool:
        """Start ending ``session`` the considerate way, if there is a reason.

        True when a wind-down is now running (or already was): the caller
        must not terminate the session itself. False when there is nothing
        to wind down — the feature is off, the board is out of reach, or the
        session holds no active issue — and the caller kills as before.
        """
        cfg = store.daemon_config()
        if not cfg.get("beads_winddown", True):
            return False
        name = session.sdef.name
        if name in self.winddowns:
            return True
        if session.exited or not self.available():
            return False
        try:
            issues = await self.active_issues(session)
        except Exception as exc:
            log.debug("beads: no wind-down for %r: %s", name, exc)
            return False
        if not issues:
            return False
        grace = float(cfg.get("beads_winddown_grace", DEFAULT_GRACE) or 0)
        if grace <= 0:
            return False
        text = compose_winddown(name, issues, grace)
        self.winddowns[name] = {
            "since": _utcnow(),
            "grace": grace,
            "issues": [i["id"] for i in issues],
            "delivered": None,
        }
        task = asyncio.ensure_future(self._winddown(session, manager, text, grace))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def _winddown(self, session: Session, manager, text: str, grace: float) -> None:
        name = session.sdef.name
        state = self.winddowns.get(name) or {}
        started = self._clock()
        try:
            try:
                delivered = await asyncio.wait_for(session.deliver(text), grace)
            except (asyncio.TimeoutError, Exception) as exc:
                log.info("beads: wind-down block not delivered to %r: %s", name, exc)
                delivered = False
            state["delivered"] = delivered
            if delivered:
                seen_busy = False
                while not session.exited and self._clock() - started < grace:
                    status = session.status()
                    if status == STATUS_BUSY:
                        seen_busy = True
                    elif status == STATUS_IDLE:
                        if seen_busy:
                            break
                        if self._clock() - started > REACT_WINDOW:
                            break  # it never picked the block up
                    await asyncio.sleep(0.5)
        finally:
            self.winddowns.pop(name, None)
            if not session.exited:
                try:
                    session.kill(force=False)
                    manager.persist()
                except Exception as exc:
                    log.warning("beads: wind-down kill of %r failed: %s", name, exc)

    async def cancel_all(self) -> None:
        """Daemon shutdown: pending wind-downs are dropped, not finished —
        the sessions come back on restart with the same names and work."""
        for task in list(self._tasks):
            task.cancel()
        for task in list(self._tasks):
            try:
                await task
            except (asyncio.CancelledError, Exception):
                pass
        self.winddowns.clear()

    # ---- exit ----------------------------------------------------------- #
    def session_exited(self, session) -> None:
        """The manager's exit hook: sweep the board for a session that is
        gone. Scheduled, never awaited — the funnel it is called from is
        synchronous and must not wait on a subprocess."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.sweep(session))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    def sessions_exited(self, sessions) -> None:
        """:meth:`session_exited` for many sessions at once -- the records a
        restart retired. One task reads each board once for all of them
        (:meth:`sweep_many`)."""
        sessions = list(sessions)
        if not sessions:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self.sweep_many(sessions))
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)

    async def sweep(self, session) -> List[List[str]]:
        """Apply :func:`sweep_plan` to the board; the writes made, for the log."""
        return (await self.sweep_many([session])).get(session.sdef.name, [])

    async def sweep_many(self, sessions) -> Dict[str, List[List[str]]]:
        """:meth:`sweep` for several sessions, reading each board once.

        A restart retires every record it does not relaunch, and each used to
        get a sweep of its own: a fresh ``br list --all`` of the whole board
        (1284 issues, 3.4 MB here) decoded on the event loop, once per record
        -- 6.2 s of loop in the first two minutes after a restart, measured
        by py-spy (claunch-fh8u1).

        Plans made from one listing can depend on each other: a delegate's
        release clears an assignee, and the issue it releases is then an
        unassigned follow-up of its creator -- who, retired in the same
        restart, planned from the listing where the delegate still held it
        and wrote nothing (claunch-aakcb). So each write is mirrored into the
        listing, and the rows it changed are planned again for the *other*
        sessions of the group until a pass changes nothing. The writer is
        left out: its own plan already saw the row, and planning it again
        from the state it wrote would release its own in_progress issue on
        top of returning it to open.

        A session whose sweep ran to the end is stamped ``swept_at``, and
        :attr:`swept_hooks` (the manager's persist) write it down at once; a
        later restart does not sweep the same ending again (claunch-fh8u1.2).
        """
        done_by: Dict[str, List[List[str]]] = {}
        if not self.available():
            return done_by
        stamped = False
        by_root: Dict[str, Tuple[Path, list]] = {}
        for session in sessions:
            try:
                root = await self.root_for(session.sdef.cwd)
            except Exception as exc:
                log.debug("beads: no sweep for %r: %s", session.sdef.name, exc)
                continue
            if root is None or not self.has_board(root):
                _mark_swept(session)  # no board, nothing this ending owes
                stamped = True
                continue
            by_root.setdefault(str(root), (root, []))[1].append(session)
        for root, group in by_root.values():
            try:
                self._cache.pop(str(root), None)
                listed = await self.issues(root)
            except Exception as exc:
                log.debug("beads: no sweep of %s: %s", root, exc)
                continue
            # Copies: the writes are mirrored into these, not into the cache.
            rows = [dict(r) for r in listed]
            by_id = {str(r.get("id") or ""): r for r in rows}
            failed: set = set()
            # Every session of a pass plans from the same rows; the pass's
            # writes are mirrored only once it is over, so nobody reads a row
            # half-way through a pass and is then asked about it again.
            last: Optional[Dict[str, set]] = None  # issue id -> who wrote it
            for _ in range(len(group) + 1):
                writes: List[Tuple[str, List[List[str]]]] = []
                for session in group:
                    name = session.sdef.name
                    if last is None:
                        subset = rows
                    else:
                        subset = [by_id[i] for i, who in last.items() if name not in who]
                        if not subset:
                            continue
                    done, ok = await self._sweep_rows(root, subset, session)
                    done_by.setdefault(name, []).extend(done)
                    if not ok:
                        failed.add(name)
                    writes.append((name, done))
                last = {}
                for name, done in writes:
                    for iid in _mirror(by_id, done):
                        last.setdefault(iid, set()).add(name)
                if not last:
                    break
            for session in group:
                if session.sdef.name not in failed:
                    _mark_swept(session)
                    stamped = True
        if stamped:
            for hook in list(self.swept_hooks):
                try:
                    hook()
                except Exception:  # the sweep itself is done; log and go on
                    log.exception("beads: swept hook %r failed", hook)
        return done_by

    async def _sweep_rows(
        self, root: Path, rows: List[dict], session
    ) -> Tuple[List[List[str]], bool]:
        """Plan and apply one session's sweep over ``rows``: the writes made,
        and whether every planned write went through."""
        name = session.sdef.name
        mine = match(rows, name, issue=session.sdef.issue, task=session.sdef.task)
        plan = sweep_plan(mine, name, exit_code=session.exit_code)
        done: List[List[str]] = []
        for args in plan:
            try:
                await self.br(root, args, actor=name)
                done.append(args)
            except cli_beads.BeadsError as exc:
                log.warning("beads: sweep of %r: %s", name, exc)
        if done:
            log.info("beads: swept %d issue write(s) for exited %r", len(done), name)
        return done, len(done) == len(plan)


def _mirror(by_id: Dict[str, dict], writes: Sequence[List[str]]) -> List[str]:
    """Apply :func:`sweep_plan`'s state writes to listed rows in place; the ids
    whose status or assignee they changed. Comments change neither."""
    changed: List[str] = []
    for args in writes:
        if len(args) < 2 or args[0] not in ("update", "close"):
            continue
        row = by_id.get(args[1])
        if row is None:
            continue
        if args[0] == "close":
            row["status"] = "closed"
        else:
            opts = args[2:]
            for flag, value in zip(opts[::2], opts[1::2]):
                if flag == "--status":
                    row["status"] = value
                elif flag == "--assignee":
                    row["assignee"] = value or None
                elif flag == "--remove-label":
                    row["labels"] = [x for x in (row.get("labels") or []) if x != value]
        changed.append(args[1])
    return changed


def _mark_swept(session) -> None:
    """Stamp the ending's sweep as made (see :meth:`Board.sweep_many`)."""
    session.swept_at = datetime.now(timezone.utc).isoformat(timespec="seconds")


def link_issue(session, issue: str) -> None:
    """Record ``issue`` on the session's definition (persisted with it)."""
    session.sdef = replace(session.sdef, issue=issue)
