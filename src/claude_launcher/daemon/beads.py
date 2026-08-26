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
ACTIVE_STATUSES = ("open", "in_progress", "in_review", "blocked")

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

#: A wind-down: how long the agent has to react to the block at all before it
#: is treated as not listening, and the ceiling on the whole turn after that.
REACT_WINDOW = 20.0
DEFAULT_GRACE = 120.0
DEFAULT_TITLE_LIMIT = 100

Runner = Callable[[List[str], str], Awaitable[Tuple[int, str, str]]]


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
    order = {"in_progress": 0, "in_review": 1, "blocked": 2, "open": 3}
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


def compose_description(task: str, *, name: str, parent: Optional[str]) -> str:
    """The description of a daemon-minted issue, in the shape the workflows
    require (목표 / 범위 / 완료 증거 기준 / 출처) so ``br lint`` and the next
    reader find the sections they expect. The task is the goal verbatim; the
    rest is left for the agent's intake to fill."""
    origin = (
        f"session {parent} (spawn)" if parent else "operator (new session)"
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
        f"{origin}, {_utcnow()}, opening task of session {name}"
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
    issue: str, *, mode: str, held_by: Optional[str] = None, mesh: str = ""
) -> str:
    """The ``issue: <id>`` line appended to a new session's opening task.

    One sentence per thing the agent has to know and cannot find out on its
    own: which record is its own, whether it is the assignee, and — when it
    is not — who to settle that with and how. The read command is spelled out
    because the workflows teach that exact call.
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
    is closed. ``in_review`` and ``blocked`` are somebody else's turn and are
    left as they are.
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
        if args and args[0] not in ("list", "show", "comments", "search"):
            self._cache.pop(key, None)
        elif args[0] == "comments" and len(args) > 1 and args[1] == "add":
            self._cache.pop(key, None)
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
        for root in order:
            entry: dict = {"root": str(root), "issues": [], "sessions": [], "error": None}
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
            result["boards"].append(entry)
        return result

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

        Never raises: a board that cannot be written must not cost a session
        its launch. Returns ``{"issue": id, "created": bool, "mode": ...,
        "held_by": name|None, "why": str}`` on success, where ``mode`` is one
        of :data:`MINTED`, :data:`TAKE`, :data:`JOIN`.
        """
        cfg = store.daemon_config()
        if not cfg.get("beads_auto_issue", True) or body.get("beads") is False:
            return None
        sdef = session.sdef
        task = str(body.get("task") or sdef.task or "")
        context = str(body.get("context") or "")
        explicit = str(body.get("issue") or sdef.issue or "").strip() or None
        refs = ([explicit] if explicit else []) + [
            r for r in issue_refs(task, context) if r != explicit
        ]
        if not refs and not task.strip():
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
            title = issue_title(task) or f"session {name}"
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
                    compose_description(task, name=name, parent=parent),
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
                "held_by": None, "why": "minted from the opening task",
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
        open_rows = [r for r in rows if r.get("status") in ACTIVE_STATUSES]
        open_rows.sort(
            key=lambda r: (
                _status_rank(r.get("status")),
                int(r.get("priority") or 9),
                -_ts(r.get("updated_at")),
            )
        )
        for raw in open_rows:
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
