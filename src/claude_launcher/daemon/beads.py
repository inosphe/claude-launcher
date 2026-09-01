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
import time
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from typing import Awaitable, Callable, Dict, List, Optional, Sequence, Tuple

from .. import cli_beads, reports as reports_mod, store
from .session import STATUS_BUSY, STATUS_IDLE, Session

log = logging.getLogger("claude_launcher.daemon.beads")

#: Issue statuses that mean "somebody still means to do this".
ACTIVE_STATUSES = ("open", "in_ready", "in_progress", "in_review", "blocked")

#: The label every issue the daemon mints carries, so the sweep can tell its
#: own placeholder from an issue an agent or a human wrote.
SESSION_LABEL = "session"

#: ``issue: <id>`` — how a task or a workflow context names the issue a
#: session is for (the improv workflows' own convention).
ISSUE_REF = re.compile(r"\bissue:\s*([A-Za-z][\w.-]*)")

#: What :func:`adoption` decided about an issue a creation request named, and
#: what :meth:`Board.ensure_issue` reports back as ``mode``. ``MINTED`` is the
#: fourth: nothing was named and an issue was created from the task.
TAKE = "assigned"
JOIN = "joined"
MINTED = "created"

#: The sender a daemon-originated ownership notice speaks as on the mesh --
#: not a member, so it is never mistaken for a peer asking for something.
BOARD_SENDER = "beads"

#: How long a board listing is trusted before it is read again. The web UI
#: polls every 2 s; without this each open rail would fork ``br`` at that rate.
CACHE_TTL = 2.0

#: The most issues an edge read will ask ``br dep list`` about in one pass.
#: The listing already says which issues have outgoing edges at all
#: (``dependency_count``), so on an ordinary board this loop runs a handful of
#: times or not once -- but ``br`` has no bulk edge dump and the per-board lock
#: serialises these, so a board that wired everything to everything would spend
#: the poll interval forking. Past the cap the hierarchy is drawn from the edges
#: that were read and the rest of the board stays flat, which is the same shape
#: a board with no edges draws; the page is never held up for it.
DEPS_SCAN_LIMIT = 200

#: A wind-down: how long the agent has to react to the block at all before it
#: is treated as not listening, and the ceiling on the whole turn after that.
REACT_WINDOW = 20.0
DEFAULT_GRACE = 120.0
DEFAULT_TITLE_LIMIT = 100

#: The actor a dashboard write is stamped with. Not a session: the Queues
#: page moves an assignment on the operator's behalf, and a comment signed by
#: the session it was moved TO would read as that session claiming the work.
DASHBOARD_ACTOR = "dashboard"

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


def compose_description(
    task: str, *, name: str, parent: Optional[str], text: bool = False
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
    """
    origin = (
        f"session {parent} (spawn)" if parent else "operator (new session)"
    )
    source = (
        f"issue text written when session {name} was created"
        if text
        else f"opening task of session {name}"
    )
    return (
        "## 목표\n"
        f"{task.strip()}\n\n"
        "## 범위(포함·제외)\n"
        "(registered at session creation by the claunch daemon — the "
        "assignee fills this in at intake)\n\n"
        "## 완료 증거 기준\n"
        "(the assignee fills this in at intake: test counts, commit hash)\n\n"
        "## 출처\n"
        f"{origin}, {_utcnow()}, {source}"
    )


class BoardRequestError(ValueError):
    """A creation request whose board answer contradicts itself."""


def check_request(body: dict) -> None:
    """Refuse a creation request that asks for two board answers at once.

    The board question has exactly one answer per session, and each of the
    three ways of giving it is a different key: ``issue_text`` writes a new
    one, ``issue`` (or an ``issue: <id>`` inside the task or context) adopts
    one that exists, ``beads: false`` asks for none. Sent together they are
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
    if body.get("beads") is False:
        raise BoardRequestError(
            "'issue_text' writes a new issue and 'beads: false' asks for "
            "none — a request cannot mean both. Send one"
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

    Only what the session was *assigned* is touched: ``in_progress`` goes
    back to ``open`` with a comment naming the exit (the leader's own orphan
    rule, done at the moment it becomes true instead of at the next board
    check); the daemon's own placeholder, still ``open`` and never taken up,
    is closed. ``in_ready`` keeps its completed triage, while ``in_review``
    and ``blocked`` are somebody else's turn; all three are left as they are.
    """
    plan: List[List[str]] = []
    code = "unknown" if exit_code is None else str(exit_code)
    for i in issues:
        if i.get("assignee") != name:
            continue
        iid = str(i.get("id") or "")
        if not iid:
            continue
        status = i.get("status")
        if status == "in_progress":
            plan.append(
                ["comments", "add", iid,
                 f"SESSION ENDED: {name} exited (code {code}); returned to open "
                 f"by the claunch daemon — reassign or resume"]
            )
            plan.append(["update", iid, "--status", "open"])
        elif (
            status == "open"
            and SESSION_LABEL in (i.get("labels") or [])
            and i.get("created_by") == name
        ):
            plan.append(
                ["close", iid, "--reason",
                 f"session {name} ended (code {code}) before taking this up"]
            )
    return plan


# --------------------------------------------------------------------------- #
# the board
# --------------------------------------------------------------------------- #
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
    ) -> None:
        self._runner = runner
        self._root_for = root_for or cli_beads.repo_root
        self._clock = clock
        self._roots: Dict[str, Optional[Path]] = {}
        self._locks: Dict[str, asyncio.Lock] = {}
        self._cache: Dict[str, Tuple[float, List[dict]]] = {}
        #: The dependency edges of a board, cached beside its listing and
        #: dropped with it -- an edge read is derived from the listing it was
        #: taken against, so keeping one past the other would draw a hierarchy
        #: out of issues that are no longer there.
        self._deps: Dict[str, Tuple[float, List[dict]]] = {}
        #: Sessions mid wind-down, by name: what was typed and when.
        self.winddowns: Dict[str, dict] = {}
        self._tasks: set = set()

    # ---- availability -------------------------------------------------- #
    def available(self) -> bool:
        """Whether ``br`` can be run at all (a fake runner counts)."""
        return self._runner is not None or shutil.which(cli_beads.BINARY) is not None

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

    @staticmethod
    def has_board(root: Optional[Path]) -> bool:
        if root is None:
            return False
        d = root / cli_beads.BEADS_DIR
        return (d / cli_beads.DB_NAME).is_file() or (d / cli_beads.JSONL_NAME).is_file()

    # ---- running br ----------------------------------------------------- #
    async def _run(self, argv: List[str], cwd: str) -> Tuple[int, str, str]:
        if self._runner is not None:
            return await self._runner(argv, cwd)
        proc = await asyncio.create_subprocess_exec(
            *argv,
            cwd=cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        out, err = await proc.communicate()
        return (
            proc.returncode or 0,
            out.decode("utf-8", "replace"),
            err.decode("utf-8", "replace"),
        )

    async def br(
        self, root: Path, args: List[str], *, actor: Optional[str] = None
    ):
        """Run one ``br`` command against ``root``'s board; parsed JSON back.

        Writes invalidate the listing cache for that board. A non-zero exit
        is a :class:`cli_beads.BeadsError` carrying ``br``'s own words.
        """
        if not self.available():
            raise BeadsUnavailable(
                f"'{cli_beads.BINARY}' is not installed on the daemon machine"
            )
        beads_dir = root / cli_beads.BEADS_DIR
        commands = cli_beads.plan(
            list(args) + (["--json"] if "--json" not in args else []),
            root,
            actor,
            db_exists=(beads_dir / cli_beads.DB_NAME).is_file(),
            jsonl_exists=(beads_dir / cli_beads.JSONL_NAME).is_file(),
        )
        key = str(root)
        lock = self._locks.setdefault(key, asyncio.Lock())
        async with lock:
            out = ""
            for cmd in commands:
                code, out, err = await self._run(cmd, str(root))
                if code != 0:
                    detail = (err or out).strip()
                    raise cli_beads.BeadsError(
                        f"br {' '.join(cmd[3:])[:80]} failed ({code}): {detail}"
                    )
        if args and not _reads_only(args):
            self._cache.pop(key, None)
            self._deps.pop(key, None)
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

    async def edges(self, root: Path, rows: Sequence[dict]) -> List[dict]:
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
        key = str(root)
        now = self._clock()
        hit = self._deps.get(key)
        if hit and now - hit[0] < CACHE_TTL:
            return hit[1]
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
        self._deps[key] = (now, out)
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
                "no board: this directory is not in a repository with a "
                ".beads/ (claunch beads init --prefix <name> at the root)"
            )
            return view
        view["root"] = str(root)
        try:
            rows = await self.issues(root)
        except cli_beads.BeadsError as exc:
            view["error"] = str(exc)
            return view
        view["issues"] = match(rows, sdef.name, issue=sdef.issue, task=sdef.task)
        wd = self.winddowns.get(sdef.name)
        if wd:
            view["winddown"] = wd
        return view

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
                "root": str(root), "issues": [], "deps": [], "sessions": [],
                "error": None,
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
                entry["issues"].append({**raw, "sessions": owners.get(raw.get("id"), [])})
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

    async def queues_view(
        self,
        sessions: Sequence,
        extra_roots: Sequence[str] = (),
        *,
        cflow_for: Optional[Callable[[str, str], Optional[dict]]] = None,
    ) -> dict:
        """Every board's queues -- the Queues tab: one lane per session (and
        per assignee the daemon does not know), each carrying the issues the
        board assigns to it in the order the worker takes them, plus the
        unassigned pool the operator drags from.

        The lanes are sessions first, in the daemon's order, then any other
        assignee an active issue names (a human, a session on another
        machine) -- a card that could not be dragged back to a lane the page
        does not draw would be stuck. An exited session with nothing assigned
        draws no lane; one that still holds issues does, so what it left
        behind can be moved. ``cflow_for(name, cwd)`` is the run summary a
        lane head shows beside the session's status (``None`` for none).
        """
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
            entry: dict = {"root": str(root), "lanes": [], "unassigned": [], "error": None}
            members = by_root[str(root)]
            try:
                rows = await self.issues(root)
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
                if s is not None and s.status() == "exited" and not queue:
                    continue
                lane: dict = {
                    "session": name,
                    "known": s is not None,
                    "status": s.status() if s is not None else None,
                    "issue": s.sdef.issue if s is not None else None,
                    "cflow": None,
                    "issues": queue,
                    "summary": queue_summary(queue),
                }
                if s is not None and cflow_for is not None:
                    try:
                        lane["cflow"] = cflow_for(name, s.sdef.cwd or "")
                    except Exception as exc:  # a run state that cannot be read
                        log.debug("beads: no cflow summary for %r: %s", name, exc)
                entry["lanes"].append(lane)
            pool = [r for r in active if not r.get("assignee")]
            pool.sort(key=_queue_rank)
            entry["unassigned"] = [dict(r) for r in pool]
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
        (``beads_auto_issue``, or ``beads: false`` on the request, which is the
        "no issue at all" answer the creation forms offer).

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
        if not cfg.get("beads_auto_issue", True) or body.get("beads") is False:
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
                        goal, name=name, parent=parent, text=bool(written)
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
        view = {"available": self.available(), "root": None, "issues": [],
                "error": None}
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
                "no board: this directory is not in a repository with a "
                ".beads/ (claunch beads init --prefix <name> at the root)"
            )
            return view
        view["root"] = str(root)
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

    async def create_for(self, session, *, title: str, description: str = "") -> dict:
        """The rail's manual create: an issue for a session that has none."""
        root = await self.root_for(session.sdef.cwd)
        if not self.has_board(root):
            raise BeadsUnavailable("no board for this session's directory")
        name = session.sdef.name
        desc = description.strip() or compose_description(
            title, name=name, parent=session.sdef.parent
        )
        data = await self.br(
            root,
            [
                "create", title, "--type", "task", "--priority", "2",
                "--labels", f"{SESSION_LABEL},user", "--assignee", name,
                "--description", desc,
            ],
            actor=name,
        )
        iid = data.get("id") if isinstance(data, dict) else None
        if not iid and isinstance(data, list) and data:
            iid = data[0].get("id")
        if not iid:
            raise cli_beads.BeadsError("br create answered without an id")
        return {"issue": str(iid), "created": True}

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

    async def sweep(self, session) -> List[List[str]]:
        """Apply :func:`sweep_plan` to the board; the writes made, for the log."""
        name = session.sdef.name
        if not self.available():
            return []
        try:
            root = await self.root_for(session.sdef.cwd)
            if not self.has_board(root):
                return []
            self._cache.pop(str(root), None)
            rows = await self.issues(root)
        except Exception as exc:
            log.debug("beads: no sweep for %r: %s", name, exc)
            return []
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
        return done


def link_issue(session, issue: str) -> None:
    """Record ``issue`` on the session's definition (persisted with it)."""
    session.sdef = replace(session.sdef, issue=issue)
