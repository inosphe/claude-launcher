"""``claunch beads ...`` — the workspace's issue board, reached from any worktree.

The board is `beads <https://github.com/steveyegge/beads>`_ driven through its
``br`` binary; this module adds nothing to it but *where it is*. ``br`` finds
its database by looking for ``.beads/*.db`` in the current directory, which
is right for a checkout and wrong for a fleet: a session working in a git
worktree has the tracked ``.beads/issues.jsonl`` there but no database, so
``br`` would quietly initialise a second, empty board in the worktree and
the fleet would end up with one board per checkout. One workspace has one
board, and every call — from whichever worktree the caller stands in —
names it with ``--db``.

Which board that is comes from :func:`resolve`, and the answer is the
registered **workspace** the caller's directory lives in (a worktree at
``<repo>/.claude/worktrees/<name>`` resolves to the repository that was
registered). Its database is ``<workspace>/.beads/beads.db`` unless the
settings point that board's name somewhere else
(:mod:`claude_launcher.beads_db`). A checkout nobody registered keeps the
board it already holds, and anything else files on
:data:`claude_launcher.beads_db.DEFAULT_BOARD`, which is the board the
daemon was using before workspaces had boards of their own.

The rest of the job: stamp writes with the calling session's name as
``--actor`` so the audit trail says which agent did what, and hand the rest
of the argument list to ``br`` untouched. Two bootstraps run before the
caller's command when the database is not there yet. When the tracked JSONL
is beside it — a fresh clone, or the main checkout right after the board's
first merge — the database is rebuilt from the JSONL (:func:`plan`). When
there is nothing to rebuild from and the board belongs to a registered
workspace or to a database path set by hand, it is created
(:func:`create_board`) under a prefix made from the board's name. Neither
step is something the caller has to know about; a directory that is merely
where someone was standing still gets the refusal it always got.

What this deliberately is not: a wrapper that re-spells ``br``'s commands.
The improv workflows teach ``claunch beads <br arguments>`` and nothing
else, so ``br``'s own ``--help`` stays the reference.

There is one exception, and it refuses rather than re-spells: a
``--status`` value that is not a status. ``br`` matches the string
literally without checking it against anything, so ``--status
in_progress,in_review`` reads as one status nothing is in and answers
``total: 0`` with exit 0 — indistinguishable from a board with nothing
active — while ``update --status in_reviw`` stores the typo and drops
that issue out of every status filter and out of ``ready``. Both are the
same failure: the answer to "I could not read your question" and the
answer to "there is nothing" arrive identical. So the value is checked
here, before ``br`` sees it. That one named check is the whole of the
validation — no other argument is judged, and this is not a place to grow
a general validation layer.

Two rewrites sit beside that check, and they change spelling rather than
meaning. ``br``'s parser reads any value that begins with ``-`` as another
option, so free text written by a person or an agent is refused whenever it
opens with one: a description carrying the workspace's YAML front matter
(``---``), an evidence bundle whose first line is a markdown list item, a
title that starts with a dash. :func:`bind_text_values` binds such a value
to its option with ``=``, and :func:`flag_text_positionals` moves such a
positional onto the flag ``br`` offers for the same text. Both leave every
other call exactly as it was written, and both stand down whenever the
argument list can be read more than one way, so what a caller gets back in
that case is ``br``'s own refusal rather than a rewritten command.
"""

from __future__ import annotations

import argparse
import os
import re
import shutil
import subprocess
import sys
from pathlib import Path
from typing import List, NamedTuple, Optional

from . import beads_db, workspaces

#: The board's directory and files, as ``br init`` lays them out. The
#: first two come from :mod:`claude_launcher.beads_db`, which composes the
#: default database path from them; they are re-exported here because
#: every caller of this module already spells them with this prefix.
BEADS_DIR = beads_db.BEADS_DIR
DB_NAME = beads_db.DB_NAME
JSONL_NAME = "issues.jsonl"
CONFIG_NAME = "config.yaml"

#: The session name every claunch-managed session carries; it becomes the
#: ``--actor`` on every ``br`` write so the audit trail names the agent.
SESSION_ENV = "CLAUNCH_SESSION"

#: The binary. ``bd`` is the older Go implementation of the same tracker
#: and reads the same files differently; it is never used here.
BINARY = "br"


class BeadsError(Exception):
    """The command cannot go to ``br`` — no root, no board, no ``br``, or a
    ``--status`` value that is not a status. The command surfaces it as
    exit 2 with the message on stderr."""


#: The statuses this repository's own protocol names. The improv
#: workflows are where they are declared — an issue opens, leader triage
#: moves it to ``in_ready``, an assignee takes it to ``in_progress``, a
#: landing request moves it to ``in_review``, a gate parks it at ``blocked``,
#: and ``br close`` ends it at ``closed`` — and every value the tracked board
#: carries is one of them (``.beads/issues.jsonl``). Those two are the source; a test in
#: ``tests/test_cli_beads.py`` reads both and fails if either grows a
#: value this tuple does not have, so the list cannot go stale quietly.
PROTOCOL_STATUSES = (
    "open",
    "in_ready",
    "in_progress",
    "in_review",
    "blocked",
    "closed",
)

#: Statuses ``br`` itself declares that the protocol above never names.
#: Accepted, so a question about an issue actually in one of them can be
#: asked — refusing a real status would be this same defect with the sides
#: swapped. Read wide, write narrow: what costs is an unreal value being
#: stored, and a real one being unaskable.
#:
#: Where each comes from, checked against **br 0.2.14** (``br <cmd>
#: --help``, ``br schema issue``):
#:
#: ==========  ============================================================
#: deferred    ``br defer`` (``br undefer`` takes it back)
#: tombstone   ``br delete`` — the tombstone it leaves behind
#: draft       no subcommand found that writes it; it appears only in the
#:             schema enum. Origin unrecorded.
#: pinned      same — schema enum only, no writer found.
#: ==========  ============================================================
#:
#: A ``br`` that changes this list changes it silently, so the source note
#: above is what a later reader checks it against. Which of the two sets is
#: authoritative is an open divergence, filed as claunch-dx5j.
UNUSED_BR_STATUSES = (
    "deferred",
    "draft",
    "tombstone",
    "pinned",
)

#: What ``--status`` is allowed to carry.
STATUSES = PROTOCOL_STATUSES + UNUSED_BR_STATUSES


def status_values(args: List[str]) -> List[str]:
    """Every value given to ``--status``/``-s`` in ``args``, in order.

    The spellings read are the ones clap accepts as a token of their own:
    ``--status V``, ``--status=V``, ``-s V``, ``-sV`` (and ``-s=V``). A
    bundled short group such as ``-as V`` is not read — telling a bundle
    from a word needs ``br``'s per-subcommand flag table, and a filter that
    goes unchecked is a smaller cost than a valid call refused. Parsing
    stops at a bare ``--``, after which everything is a positional.
    """
    values: List[str] = []
    i = 0
    while i < len(args):
        token = args[i]
        if token == "--":
            break
        if token in ("--status", "-s"):
            if i + 1 < len(args):
                values.append(args[i + 1])
            i += 2
            continue
        if token.startswith("--status="):
            values.append(token[len("--status=") :])
        elif token.startswith("-s") and not token.startswith("--") and len(token) > 2:
            values.append(token[2:].lstrip("="))
        i += 1
    return values


def check_statuses(args: List[str]) -> None:
    """Refuse a ``--status`` value that is not one of :data:`STATUSES`.

    Raises :class:`BeadsError`, which the command surfaces as exit 2. A
    value carrying a comma or a space gets the extra line, because that is
    the shape the workflows kept writing and the one whose silent answer
    cost the most: several statuses are a repeated flag, not a list.
    """
    for value in status_values(args):
        if value in STATUSES:
            continue
        hint = ""
        if "," in value or " " in value:
            hint = (
                " — several statuses are a repeated flag, not a list: "
                "'--status open --status in_progress'"
            )
        raise BeadsError(
            f"unknown status {value!r}{hint}; "
            f"valid: {', '.join(STATUSES)}"
        )


#: Options whose value is free text — a description, a title, a close
#: reason, a comment body. What they carry is written by a person or an
#: agent, so it may begin with ``-``, and ``br``'s parser reads a value that
#: begins with ``-`` as another option: a description that opens with a
#: YAML front matter fence (``---``) is refused with ``unexpected
#: argument '---...' found`` before anything is written. That is how
#: every issue the daemon minted in a registered workspace failed: the
#: workspace front matter opens with that fence
#: (:mod:`claude_launcher.beads_meta`), and the create never ran.
#: :func:`bind_text_values` binds those values to their option with ``=``,
#: the one spelling clap reads as a value whatever it starts with.
#:
#: Every spelling of each option is listed, aliases and short forms
#: included, because the pair is recognised by an exact match. Read against
#: **br 0.2.14** (``br create|update|close|comments add --help``). An option
#: missing from this tuple is not broken — it only keeps the behaviour it
#: had, which is a refusal when its value begins with ``-``.
TEXT_OPTIONS = (
    "--description", "-d", "--body",
    "--title",
    "--design",
    "--acceptance-criteria", "--acceptance",
    "--notes",
    "--reason", "-r",
    "--bypass-reason",
    "--message",
)


def bind_text_values(args: List[str]) -> List[str]:
    """``args`` with each free-text option value bound to its option by ``=``.

    Only a value that begins with ``-`` is bound, so an ordinary call keeps
    the argument list it always had and a failing ``br`` command still reads
    the way it was written. Parsing stops at a bare ``--``: everything after
    it is a positional and none of it is an option's value.

    What this does not reach: a *positional* that begins with ``-`` — a
    comment body whose first line is a markdown list item, as
    ``comments add <id> "- branch: x"``. There is no ``=`` to bind a
    positional with. :func:`flag_text_positionals` handles that case
    separately, by moving the text onto the flag ``br`` offers for it.
    """
    bound: List[str] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            bound.extend(args[index:])
            break
        if (
            token in TEXT_OPTIONS
            and index + 1 < len(args)
            and args[index + 1].startswith("-")
        ):
            bound.append(f"{token}={args[index + 1]}")
            index += 2
            continue
        bound.append(token)
        index += 1
    return bound


#: The options ``br`` carries on every subcommand. They are split by whether
#: they consume the token after them, because that is the only thing
#: :func:`_positional_slots` needs from them: an option that takes a value
#: hides the token behind it, and reading that token as a positional is how
#: a scan goes wrong. Read against **br 0.2.14**.
GLOBAL_VALUE_OPTIONS = ("--db", "--actor", "--lock-timeout")
GLOBAL_FLAGS = (
    "--json", "--no-daemon", "--no-auto-flush", "--no-auto-import",
    "--allow-stale", "--no-db", "--verbose", "-v", "--quiet", "-q",
    "--no-color", "--help", "-h",
)

#: The same split for ``br create`` (``br create --help``, br 0.2.14).
CREATE_VALUE_OPTIONS = GLOBAL_VALUE_OPTIONS + (
    "--title",
    "--type", "-t",
    "--slug",
    "--priority", "-p",
    "--description", "-d", "--body",
    "--assignee", "-a",
    "--owner",
    "--labels", "-l",
    "--parent",
    "--deps",
    "--estimate", "-e",
    "--due",
    "--defer",
    "--external-ref",
    "--status", "-s",
    "--file", "-f",
)
CREATE_FLAGS = GLOBAL_FLAGS + ("--ephemeral", "--dry-run", "--silent")

#: And for ``br comments add`` (``br comments add --help``, br 0.2.14).
COMMENTS_ADD_VALUE_OPTIONS = GLOBAL_VALUE_OPTIONS + (
    "--file", "-f",
    "--author",
    "--message",
)
COMMENTS_ADD_FLAGS = GLOBAL_FLAGS


def _positional_slots(
    args: List[str], value_options, flags
) -> Optional[List[int]]:
    """Indices in ``args`` that ``br`` would read as positional arguments.

    ``None`` means the question cannot be answered here: a bare ``--`` was
    found, after which everything is a positional and the call already
    reaches ``br`` intact.

    A token that begins with ``-`` and is in neither table is counted as a
    positional, because that is exactly the case being looked for — ``br``'s
    parser would read it as an option and refuse the command. The cost of
    the tables being incomplete is therefore a real option counted as a
    positional, and :func:`flag_text_positionals` guards against acting on
    that by refusing to transform a call whose slots do not have the shape
    the subcommand declares.
    """
    slots: List[int] = []
    index = 0
    while index < len(args):
        token = args[index]
        if token == "--":
            return None
        if token.startswith("-") and token != "-":
            name = token.split("=", 1)[0]
            if name in value_options:
                index += 1 if "=" in token else 2
                continue
            if name in flags:
                index += 1
                continue
        slots.append(index)
        index += 1
    return slots


def flag_text_positionals(args: List[str]) -> List[str]:
    """``args`` with positional free text moved onto the flag ``br`` offers.

    Two subcommands take their free text as a positional argument, and a
    positional that begins with ``-`` is read by ``br``'s parser as an
    option and refuses the command: ``create "-로 시작하는 제목"`` and
    ``comments add <id> "- branch: x"``. The second is an ordinary input in
    this repository, because the workflows ask for evidence bundles written
    as lines of ``(axis, tree, value)`` and such a line opens with ``-``.

    ``br`` 0.2.14 offers a flag for both — ``create --title`` and
    ``comments add --message`` — and a flag's value is bound with ``=``,
    which clap reads as a value whatever it starts with. So the text is
    moved there. Both flags are already in :data:`TEXT_OPTIONS`, and the
    positional and its flag are mutually exclusive in ``br``, so the
    positional is removed rather than kept alongside.

    Only a call whose slots have the shape the subcommand declares is
    transformed — one title for ``create``, an id followed by a contiguous
    run of text for ``comments add`` — and only when one of those text
    slots begins with ``-``. Anything else is handed on unchanged and gets
    whatever ``br`` makes of it, which for the failing shapes is the same
    refusal as before with ``br``'s own message in front of the caller.
    That is the deliberate fallback for an option this module's tables do
    not know: an unknown option lands in a slot, the shape stops matching,
    and nothing is rewritten.

    Text in several positionals is joined with one space, which is what
    ``br`` itself does with them (``comments add <id> "alpha" "beta"``
    stores ``"alpha beta"``).
    """
    if args[:1] == ["create"]:
        head, flag = 1, "--title"
        value_options, flags = CREATE_VALUE_OPTIONS, CREATE_FLAGS
        id_first = False
    elif args[:2] == ["comments", "add"]:
        head, flag = 2, "--message"
        value_options, flags = COMMENTS_ADD_VALUE_OPTIONS, COMMENTS_ADD_FLAGS
        id_first = True
    else:
        return args

    rest = args[head:]
    # The text is already somewhere else: on its own flag, or in a file.
    for token in rest:
        if token.split("=", 1)[0] in (flag, "--file", "-f"):
            return args

    slots = _positional_slots(rest, value_options, flags)
    if slots is None:
        return args
    if id_first:
        # The issue id is the first positional and is never the text.
        if not slots or rest[slots[0]].startswith("-"):
            return args
        text_slots = slots[1:]
    else:
        # ``create`` declares one positional. More means a token was read
        # as a positional that is not one.
        if len(slots) != 1:
            return args
        text_slots = slots
    if not text_slots:
        return args
    if not any(rest[i].startswith("-") for i in text_slots):
        return args
    if text_slots != list(range(text_slots[0], text_slots[-1] + 1)):
        return args

    joined = " ".join(rest[i] for i in text_slots)
    moved = (
        rest[: text_slots[0]]
        + [f"{flag}={joined}"]
        + rest[text_slots[-1] + 1 :]
    )
    return args[:head] + moved


def repo_root(cwd: Optional[str] = None) -> Optional[Path]:
    """The directory that owns the board for ``cwd``, or ``None``.

    Git first: ``--git-common-dir`` is the one answer that is the same from
    the main checkout and from every worktree cut from it, which is exactly
    the property a single board needs. The registered workspaces are the
    fallback for a directory git does not claim — ``workspaces.owning`` was
    written for the same containment question.
    """
    here = cwd or os.getcwd()
    try:
        proc = subprocess.run(
            ["git", "rev-parse", "--path-format=absolute", "--git-common-dir"],
            cwd=here, capture_output=True, text=True, check=False,
            encoding="utf-8", errors="replace",
        )
    except OSError:
        proc = None
    if proc is not None and proc.returncode == 0 and proc.stdout.strip():
        return Path(proc.stdout.strip()).parent
    owner = workspaces.owning(here)
    if owner is not None:
        return Path(owner.path)
    return None


def resolve(cwd: Optional[str] = None) -> Optional[beads_db.BoardRef]:
    """The board a directory files its issues on, or ``None``.

    Three rules, in this order:

    1. **The registered workspace the directory lives in.** Its board is
       ``<workspace>/.beads/beads.db`` unless ``beads.boards.<name>`` says
       otherwise (:mod:`claude_launcher.beads_db`). This is first, and not
       git, because a workspace registered *inside* another checkout
       (``design-projection`` under ``gds5``) is a separate body of work with
       a board of its own; asking git would hand both of them the outer
       repository's board. A worktree answers the same way it did before —
       ``workspaces.owning`` resolves ``<repo>/.claude/worktrees/<name>`` to
       the repository that was registered.
    2. **A git repository that already holds a board.** A checkout nobody
       registered keeps the board it has rather than having its issues filed
       somewhere else.
    3. **:data:`claude_launcher.beads_db.DEFAULT_BOARD`, for a directory
       inside its root.** It is the board the daemon was already using,
       pinned at startup
       (:func:`claude_launcher.beads_db.ensure_default`), so a directory in
       that tree that rules 1 and 2 do not claim keeps filing where it has
       been filing and nothing that was filed before has moved.

       The containment test is what keeps the rule from reaching further
       than that. Without it every directory on the machine with no board of
       its own — a scratch directory, a checkout of an unrelated project,
       a test's temporary tree — would file its issues into this one board.
       Refusing those is the answer they got before boards were per
       workspace, and it is the answer they get now.

    ``None`` means no rule matched: the caller is standing somewhere no
    board can be derived from. Registering the directory as a workspace is
    what gives it one.
    """
    here = cwd or os.getcwd()
    owner = workspaces.owning(here)
    if owner is not None:
        return beads_db.workspace_ref(owner)
    root = repo_root(here)
    if root is not None:
        beads_dir = root / BEADS_DIR
        if (beads_dir / DB_NAME).is_file() or (beads_dir / JSONL_NAME).is_file():
            return beads_db.plain_ref(root)
    fallback = beads_db.default_ref(root)
    if fallback is not None and beads_db.within(here, fallback.root):
        return fallback
    return None


#: What ``br init`` will and will not do, measured against **br 0.2.14**, and
#: the reason creating a board is a step of its own rather than another argv
#: in :func:`plan`:
#:
#: * ``init`` **ignores** ``--db``. It always writes
#:   ``<cwd>/.beads/beads.db``, whatever path the option names.
#: * ``init`` refuses when ``<cwd>/.beads/beads.db`` already **exists**
#:   ("Already initialized"). A ``.beads/`` directory without the database —
#:   a fresh clone carrying only the tracked ``issues.jsonl`` — is fine,
#:   which is what keeps the rebuild path in :func:`plan` working.
#: * every other subcommand honours ``--db`` for any filename, but still
#:   needs a ``.beads/`` directory discoverable from ``<cwd>``: without one
#:   it answers ``NOT_INITIALIZED`` whatever ``--db`` says.
#:
#: So a board's root always holds a ``.beads/``, and what the setting moves
#: is which database file is read. Creating a board that is set to another
#: path therefore runs ``init`` where ``br`` will accept it and then moves
#: the file ``br`` wrote onto the path the board is set to.
class InitPlan(NamedTuple):
    """How to bring one board's database into being — pure, so a test can
    read it without a filesystem."""

    #: The ``br init`` to run, and the directory to run it in.
    argv: List[str]
    cwd: str
    #: The file ``br`` will have written, to be moved onto the board's own
    #: path. ``None`` when ``br`` already writes it where the board is.
    move_from: Optional[str]
    #: A directory made only to hold that ``init``, to remove afterwards.
    discard: Optional[str]


#: The directory an ``init`` is staged in when ``br`` would refuse to run it
#: in the board's own root. Inside the root, so the move onto the board's
#: path stays on one volume.
STAGING_DIR = ".claunch-board-init"

#: SQLite's sidecars. A database moved without them loses whatever the
#: write-ahead log still holds.
DB_SIDECARS = ("-wal", "-shm", "-journal")


def init_plan(ref: beads_db.BoardRef, *, default_db_exists: bool) -> InitPlan:
    """The steps that create ``ref``'s database. See the note above.

    ``default_db_exists`` is whether ``<root>/.beads/beads.db`` is already a
    file — the one state ``br init`` refuses, and the only reason this needs
    a staging directory.
    """
    argv = [BINARY, "init", "--prefix", beads_db.prefix_for(ref.name)]
    root = Path(ref.root)
    written = root / BEADS_DIR / DB_NAME
    if not default_db_exists:
        return InitPlan(
            argv=argv,
            cwd=str(root),
            move_from=None if beads_db.same_path(str(written), ref.db) else str(written),
            discard=None,
        )
    staging = root / STAGING_DIR
    return InitPlan(
        argv=argv,
        cwd=str(staging),
        move_from=str(staging / BEADS_DIR / DB_NAME),
        discard=str(staging),
    )


def move_db(src: str, dst: str) -> None:
    """Move a database and its SQLite sidecars onto ``dst``."""
    shutil.move(src, dst)
    for suffix in DB_SIDECARS:
        beside = Path(src + suffix)
        if beside.is_file():
            shutil.move(str(beside), dst + suffix)


def _subprocess_runner(argv: List[str], cwd: str):
    """:func:`create_board`'s runner for the command line — the daemon hands
    in its own so the board is created through the same code path."""
    # br writes UTF-8; the locale codec (cp949 here) would kill the reader
    # thread on the first Korean byte and hand back partial output
    # (claunch-gds6-subprocess-decode-cp949-ja5ih).
    proc = subprocess.run(
        argv, cwd=cwd, check=False, capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    return proc.returncode, proc.stdout, proc.stderr


def create_board(ref: beads_db.BoardRef, runner) -> None:
    """Create ``ref``'s database. ``runner(argv, cwd)`` -> ``(code, out, err)``.

    The filesystem half of :func:`init_plan`: make the directories ``br``
    and the board need, run the ``init``, move the file into place, and drop
    the staging directory. A non-zero exit raises :class:`BeadsError` with
    ``br``'s own words and nothing is moved.
    """
    Path(ref.root).mkdir(parents=True, exist_ok=True)
    Path(ref.db).parent.mkdir(parents=True, exist_ok=True)
    plan_ = init_plan(
        ref, default_db_exists=(Path(ref.root) / BEADS_DIR / DB_NAME).is_file()
    )
    if plan_.discard:
        Path(plan_.cwd).mkdir(parents=True, exist_ok=True)
    try:
        code, out, err = runner(plan_.argv, plan_.cwd)
        if code != 0:
            raise BeadsError(
                f"creating the board {ref.name} at {ref.db} failed: "
                f"{(err or out or '').strip()}"
            )
        if plan_.move_from:
            move_db(plan_.move_from, ref.db)
    finally:
        if plan_.discard:
            shutil.rmtree(plan_.discard, ignore_errors=True)


def autocreatable(ref: beads_db.BoardRef) -> bool:
    """Whether a missing database for ``ref`` may be created on the spot.

    Only a registered workspace qualifies. Registering a directory is the
    operator saying a board belongs there, so the first command that needs
    one may build it.

    Two kinds of root are left out. A plain repository root is wherever the
    caller happened to be standing, and a typo there must not grow a board
    of its own — that refusal is the one :func:`plan` has always given.
    :data:`claude_launcher.beads_db.DEFAULT_BOARD` is left out for the same
    reason: every directory in no registered workspace resolves to it, so
    creating it on the spot would mean any such directory mints issues into
    a board the operator has not asked for. When its database is genuinely
    missing, the Settings page's own Create button builds it.
    """
    return bool(ref.workspace)


def issue_prefix(beads_dir: Path, root: Path) -> str:
    """The prefix issue ids carry, from ``config.yaml`` or the root's name.

    ``br init`` writes the prefix into the database and leaves it as a
    *comment* in ``config.yaml``; this repository uncomments it so a rebuild
    from JSONL can hand the same prefix back to ``br init``. Without it the
    rebuilt board would mint ids under a different prefix than the ones
    already in the JSONL.
    """
    config = beads_dir / CONFIG_NAME
    if config.is_file():
        for line in config.read_text(encoding="utf-8").splitlines():
            m = re.match(r"^\s*issue_prefix\s*:\s*(\S+)\s*$", line)
            if m:
                return m.group(1).strip("'\"")
    slug = re.sub(r"[^a-z0-9]+", "-", root.name.lower()).strip("-")
    return slug or "issue"


def plan(
    args: List[str],
    root: Path,
    actor: Optional[str],
    db_exists: bool,
    jsonl_exists: bool,
    *,
    db: Optional[str] = None,
) -> List[List[str]]:
    """The ``br`` invocations to run, in order — pure, so a test can read it.

    Every command names the board with ``--db``. A write gets ``--actor``
    from the session unless the caller set one. ``init`` passes straight
    through (it is how a board is first made). Anything else against a
    missing database rebuilds it from the tracked JSONL when that exists,
    and refuses when nothing is there to rebuild from — a typo'd directory
    must not grow a board of its own. A ``--status`` value that is not a
    status is refused here too, before any ``br`` runs.

    ``db`` is the database file. Left out, it is the one
    :mod:`claude_launcher.beads_db` resolves for ``root`` — the default
    ``<root>/.beads/beads.db`` unless this board has been pointed elsewhere
    in the settings. Callers that already hold a
    :class:`claude_launcher.beads_db.BoardRef` pass its path rather than
    having it resolved a second time.

    Creating a board that does not exist yet is NOT here: ``br init``
    ignores ``--db`` and writes where it stands, so it is a step with a
    filesystem move in it rather than another argv (:func:`create_board`).
    The caller runs that first, for a board the operator vouched for, and
    what reaches this function is a database that exists.

    The caller's arguments also go through two rewrites that make free text
    beginning with ``-`` reach ``br`` as text: :func:`bind_text_values` for
    text given to an option, :func:`flag_text_positionals` for text given as
    a positional argument. Both are done here rather than at each caller
    because this is the one place every ``br`` invocation is composed, and
    the first removes a failure the callers cannot see: ``br`` refuses the
    command at parse time and the daemon logs a warning nobody reads.

    Order matters between them only in that the second reads the result of
    the first. ``create --title "-x"`` is bound to ``create --title=-x`` and
    then left alone, because its text already has a flag.
    """
    check_statuses(args)
    args = flag_text_positionals(bind_text_values(args))
    beads_dir = root / BEADS_DIR
    db = str(db) if db else str(beads_db.db_for_root(root))
    base = [BINARY, "--db", db]
    if actor and "--actor" not in args:
        base += ["--actor", actor]
    if args and args[0] == "init":
        return [base + args]
    if db_exists:
        return [base + args]
    if jsonl_exists:
        prefix = issue_prefix(beads_dir, root)
        return [
            [BINARY, "--db", db, "init", "--prefix", prefix],
            [BINARY, "--db", db, "sync", "--import-only"],
            base + args,
        ]
    raise BeadsError(
        f"no board at {beads_dir} — start one at the repository root with "
        f"'claunch beads init --prefix <name>'"
    )


def run(args: List[str], cwd: Optional[str] = None) -> int:
    """Resolve the board for ``cwd`` and run ``br`` with ``args`` against it."""
    if shutil.which(BINARY) is None:
        raise BeadsError(
            f"'{BINARY}' is not installed — the board needs the beads CLI "
            f"(cargo install beads-rust, or see https://github.com/steveyegge/beads)"
        )
    ref = resolve(cwd)
    if ref is None:
        raise BeadsError(
            "not inside a git repository or a registered workspace, and no "
            f"'{beads_db.DEFAULT_BOARD}' board is set — register the "
            "directory with 'claunch workspace add <dir>', or set a board "
            "for it on the Settings page"
        )
    root = ref.root_path
    beads_dir = root / BEADS_DIR
    # A board the operator vouched for is brought into being before anything
    # is planned against it, because creating one is not an argv: see
    # InitPlan. A checkout nobody registered still gets plan()'s refusal.
    if (
        not ref.exists()
        and not (beads_dir / JSONL_NAME).is_file()
        and autocreatable(ref)
        and (args[:1] != ["init"] if args else True)
    ):
        create_board(ref, _subprocess_runner)
    commands = plan(
        list(args), root, os.environ.get(SESSION_ENV) or None,
        db_exists=ref.exists(),
        jsonl_exists=(beads_dir / JSONL_NAME).is_file(),
        db=ref.db,
    )
    code = 0
    for i, cmd in enumerate(commands):
        # Bootstrap steps run quietly; only the caller's own command keeps
        # its output. A failed bootstrap step stops the chain — running the
        # caller's command against a half-built board is worse than no
        # answer.
        last = i == len(commands) - 1
        proc = subprocess.run(
            cmd, cwd=cwd or os.getcwd(), check=False,
            capture_output=not last, text=True,
            encoding="utf-8", errors="replace",
        )
        code = proc.returncode
        if code != 0:
            if not last:
                detail = (proc.stderr or proc.stdout or "").strip()
                # Two bootstraps share this loop and they fail for different
                # reasons, so they are named apart: a rebuild reads the
                # tracked JSONL, a first board reads nothing.
                what = (
                    f"rebuilding the board from {JSONL_NAME}"
                    if (beads_dir / JSONL_NAME).is_file()
                    else f"creating the board for {ref.name} at {ref.db}"
                )
                print(
                    f"error: {what} failed at {' '.join(cmd[3:])}: {detail}",
                    file=sys.stderr,
                )
            break
    return code


def _cmd(args: argparse.Namespace) -> int:
    try:
        return run(list(args.args))
    except BeadsError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 2


def register(sub) -> None:
    p = sub.add_parser(
        "beads",
        help="the workspace's issue board (br), from any worktree",
        description=(
            "Run 'br' against this workspace's one board: the database at "
            "<workspace>/.beads/beads.db, or wherever the Settings page has "
            "pointed that board, so a worktree uses the same board as the "
            "main checkout. A directory in no registered workspace uses the "
            "checkout's own board if it has one, and the 'claunch-default' "
            "board otherwise. Writes are stamped --actor $CLAUNCH_SESSION. "
            "Every argument after 'beads' goes to br as-is; 'br --help' "
            "lists them."
        ),
    )
    p.add_argument(
        "args", nargs=argparse.REMAINDER,
        help="br subcommand and its arguments (e.g. 'ready --json')",
    )
    p.set_defaults(func=_cmd)
