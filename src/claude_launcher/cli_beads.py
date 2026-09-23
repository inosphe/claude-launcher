"""``claunch beads ...`` — the repository's issue board, reached from any worktree.

The board is `beads <https://github.com/steveyegge/beads>`_ driven through its
``br`` binary; this module adds nothing to it but *where it is*. ``br`` finds
its database by looking for ``.beads/*.db`` in the current directory, which
is right for a checkout and wrong for a fleet: a session working in a git
worktree has the tracked ``.beads/issues.jsonl`` there but no database, so
``br`` would quietly initialise a second, empty board in the worktree and
the fleet would end up with one board per checkout. One repository has one
board. It lives at the repository's main checkout, and every call — from
whichever worktree the caller stands in — names it with ``--db``.

That is the whole job here: resolve the repository root through git's
common dir (a worktree's common dir *is* the main checkout's ``.git``),
point ``--db`` at ``<root>/.beads/beads.db``, stamp writes with the calling
session's name as ``--actor`` so the audit trail says which agent did what,
and hand the rest of the argument list to ``br`` untouched. When the
database is missing but the tracked JSONL is there — a fresh clone, or the
main checkout right after the board's first merge — the database is rebuilt
from the JSONL first, so the caller never has to know that step exists.

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
from typing import List, Optional

from . import workspaces

#: The board's directory and files, as ``br init`` lays them out.
BEADS_DIR = ".beads"
DB_NAME = "beads.db"
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
) -> List[List[str]]:
    """The ``br`` invocations to run, in order — pure, so a test can read it.

    Every command names the board with ``--db``. A write gets ``--actor``
    from the session unless the caller set one. ``init`` passes straight
    through (it is how a board is first made). Anything else against a
    missing database rebuilds it from the tracked JSONL when that exists,
    and refuses when nothing is there to rebuild from — a typo'd directory
    must not grow a board of its own. A ``--status`` value that is not a
    status is refused here too, before any ``br`` runs.

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
    db = str(beads_dir / DB_NAME)
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
    root = repo_root(cwd)
    if root is None:
        raise BeadsError(
            "not inside a git repository or a registered workspace — "
            "the board belongs to a repository"
        )
    beads_dir = root / BEADS_DIR
    commands = plan(
        list(args), root, os.environ.get(SESSION_ENV) or None,
        db_exists=(beads_dir / DB_NAME).is_file(),
        jsonl_exists=(beads_dir / JSONL_NAME).is_file(),
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
                print(
                    f"error: rebuilding the board from {JSONL_NAME} failed "
                    f"at {' '.join(cmd[3:])}: {detail}",
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
        help="the repository's issue board (br), from any worktree",
        description=(
            "Run 'br' against this repository's one board: the database at "
            "<repo root>/.beads/beads.db, found through git's common dir so a "
            "worktree uses the same board as the main checkout. Writes are "
            "stamped --actor $CLAUNCH_SESSION. Every argument after 'beads' "
            "goes to br as-is; 'br --help' lists them."
        ),
    )
    p.add_argument(
        "args", nargs=argparse.REMAINDER,
        help="br subcommand and its arguments (e.g. 'ready --json')",
    )
    p.set_defaults(func=_cmd)
