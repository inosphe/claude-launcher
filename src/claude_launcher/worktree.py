"""Git worktrees for a launch: ask once, branch off, start the agent there.

``claunch run`` and ``claunch new-session`` both put an agent in a directory,
and that directory is usually the repository the human is standing in. Two
agents standing in the *same* checkout is the failure this module exists to
prevent: they edit each other's files mid-edit, one's build races the other's,
and a branch switch by either one silently rewrites what the other is looking
at.

A git worktree is the cheap fix -- a second checkout of the same repository on
its own branch, sharing one object store. So both commands offer one at the
moment the agent is created, which is the only moment the answer is free: once
an agent has read files and started editing, moving it costs the session.

**The offer is a question, and only a human is ever asked it.** Most runs are
one person's own agent in their own checkout, and silently relocating that
would be a worse surprise than the race it prevents. So the question is put to
an interactive terminal with a person behind it, and to nobody else --
``--worktree[=NAME]`` and ``--no-worktree`` answer ahead of time for everyone
who already knows, including every caller that cannot be asked. See
:func:`interactive` for who counts as a person.

Worktrees live under ``<repo>/.claude/worktrees/<name>`` -- beside the ones
Claude Code makes itself, so one ``git worktree list`` shows every checkout an
agent is working in, whoever created it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import List, Optional, Union

from . import herdr

#: Where worktrees are created, relative to the repository root. The same path
#: Claude Code's own worktree tool uses, so both kinds list together.
WORKTREES_SUBDIR = Path(".claude") / "worktrees"

#: Absolute (or repo-relative) override for that directory.
WORKTREE_DIR_ENV = "CLAUNCH_WORKTREE_DIR"

#: Set by the daemon in every managed session: an agent is at the keyboard,
#: not a person, even though the PTY looks exactly like a terminal.
SESSION_ENV = "CLAUNCH_SESSION"

#: What a name may contain. Slashes are allowed because ``feature/x`` is an
#: ordinary branch name and git nests the directory happily; the rest is
#: narrowed to what is safe in a path and quote-free in a shell.
_NAME_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._/-]*$")

#: A ``--worktree`` answer: a name, ``""`` for "yes, name it for me",
#: :data:`NEVER` for ``--no-worktree``, :data:`ASK` for "nobody said".
#: Spelled as a type because three commands hand it around.
Choice = Optional[Union[str, bool]]

#: Nobody said. The only value that can lead to a question.
ASK: Choice = None

#: What ``--no-worktree`` resolves to.
NEVER: Choice = False


class WorktreeError(Exception):
    """Raised when a worktree was asked for and could not be made."""


@dataclass(frozen=True)
class Worktree:
    """A checkout a launch was moved into."""

    path: Path
    name: str
    branch: str
    #: False when an existing worktree of that name was reused.
    created: bool

    @property
    def label(self) -> str:
        """How this launch reads on a pane label or a status line."""
        return herdr.launch_label(self.name, self.branch)


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #
def _git(args: List[str], *, cwd: str) -> subprocess.CompletedProcess:
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
        )
    except FileNotFoundError as exc:
        raise WorktreeError(
            "could not find 'git' on PATH, and a worktree needs it"
        ) from exc
    except OSError as exc:
        raise WorktreeError(f"could not run git: {exc}") from exc


def repo_root(cwd: str) -> Optional[Path]:
    """Root of the repository containing ``cwd``, or ``None`` if there is none.

    Resolved through the *common* git dir, so launching from inside an
    existing worktree puts the new one beside its siblings rather than nesting
    it inside the checkout an agent is already editing.
    """
    if not cwd or not os.path.isdir(cwd):
        return None
    done = _git(["rev-parse", "--path-format=absolute", "--git-common-dir"], cwd=cwd)
    if done.returncode != 0:
        return None
    common = (done.stdout or "").strip()
    if not common:
        return None
    git_dir = Path(common)
    if git_dir.name != ".git":
        # A bare repository has no checkout to branch from.
        return None
    return git_dir.parent


def _branch_exists(root: Path, name: str) -> bool:
    return _git(
        ["show-ref", "--verify", "--quiet", f"refs/heads/{name}"], cwd=str(root)
    ).returncode == 0


def _same_path(a: Path, b: Path) -> bool:
    def key(p: Path) -> str:
        try:
            p = p.resolve()
        except OSError:
            p = Path(os.path.abspath(str(p)))
        return os.path.normcase(str(p))

    return key(a) == key(b)


def _registered(root: Path, path: Path) -> bool:
    """Whether ``path`` is already a worktree of this repository."""
    done = _git(["worktree", "list", "--porcelain"], cwd=str(root))
    if done.returncode != 0:
        return False
    for line in (done.stdout or "").splitlines():
        if line.startswith("worktree ") and _same_path(
            Path(line[len("worktree "):].strip()), path
        ):
            return True
    return False


def current_branch(cwd: Path) -> str:
    """Branch checked out in ``cwd``; empty on a detached HEAD or on error."""
    done = _git(["rev-parse", "--abbrev-ref", "HEAD"], cwd=str(cwd))
    if done.returncode != 0:
        return ""
    branch = (done.stdout or "").strip()
    return "" if branch == "HEAD" else branch


# --------------------------------------------------------------------------- #
# naming
# --------------------------------------------------------------------------- #
def pane_token() -> str:
    """The identity half of a generated name: which pane asked for this.

    Herdr injects ``HERDR_PANE_ID`` (``w4:p4``) into every pane it manages,
    which is the pane's name as the rest of the toolchain spells it. Outside
    Herdr the managed session's own name is the next best answer, and outside
    both there is only a constant.
    """
    pane = herdr.pane_id()
    if pane:
        token = herdr.sanitize_fragment(pane)
        if token:
            return token
    session = herdr.sanitize_fragment(os.environ.get(SESSION_ENV, ""))
    return session or "wt"


def default_name(now: Optional[datetime] = None) -> str:
    """The name used when a human wants a worktree but not to name one.

    Pane plus timestamp: unique per pane per second, and legible afterwards --
    ``git worktree list`` then says which pane made each checkout, and when.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return f"{pane_token()}-{stamp}"


def validate_name(name: str) -> str:
    """``name`` if it works as both a directory and a branch, else raise."""
    name = (name or "").strip().strip("/")
    if not name:
        raise WorktreeError("worktree name is empty")
    if not _NAME_RE.match(name) or ".." in name or "//" in name:
        raise WorktreeError(
            f"invalid worktree name {name!r}: letters, digits, '.', '_', '-' "
            "and '/' only, starting with a letter or digit"
        )
    return name


# --------------------------------------------------------------------------- #
# creating
# --------------------------------------------------------------------------- #
def worktrees_dir(root: Path) -> Path:
    """Directory holding this repository's launcher worktrees."""
    override = os.environ.get(WORKTREE_DIR_ENV)
    if override:
        expanded = Path(override).expanduser()
        return expanded if expanded.is_absolute() else root / expanded
    return root / WORKTREES_SUBDIR


def create(root: Path, name: str) -> Worktree:
    """Create -- or reuse -- the worktree ``name`` of the repository at ``root``.

    Reuse is deliberate: ``--worktree=review`` a second time should return to
    that checkout with its branch and its uncommitted work intact, not fail
    because the directory is already there.
    """
    name = validate_name(name)
    path = worktrees_dir(root) / name
    if path.exists():
        if not _registered(root, path):
            raise WorktreeError(
                f"{path} already exists and is not a worktree of this repository"
            )
        return Worktree(
            path=path, name=name, branch=current_branch(path) or name, created=False
        )
    path.parent.mkdir(parents=True, exist_ok=True)
    # An existing branch is checked out as it stands (resuming named work); a
    # new one is cut from HEAD. `git worktree add` refuses either if the branch
    # is checked out somewhere else, which is the refusal we want.
    reused_branch = _branch_exists(root, name)
    args = (
        ["worktree", "add", str(path), name]
        if reused_branch
        else ["worktree", "add", "-b", name, str(path)]
    )
    done = _git(args, cwd=str(root))
    if done.returncode != 0:
        detail = (done.stderr or done.stdout or "").strip()
        raise WorktreeError(f"git worktree add failed: {detail}")
    return Worktree(
        path=path, name=name, branch=current_branch(path) or name, created=True
    )


# --------------------------------------------------------------------------- #
# deciding
# --------------------------------------------------------------------------- #
def interactive() -> bool:
    """Whether there is a *person* who can be asked a question right now.

    A tty is necessary and not sufficient. Every managed session runs on a
    PTY, so an agent's stdin is a terminal by every test Python can make --
    and a prompt printed into one is not answered, it hangs the launch until
    somebody notices. ``$CLAUNCH_SESSION`` is what distinguishes them, and the
    same check keeps the daemon, the web UI and any script out of the question
    too: none of them set it, but none of them have a tty either.
    """
    if os.environ.get(SESSION_ENV):
        return False
    try:
        return bool(sys.stdin.isatty() and sys.stderr.isatty())
    except (AttributeError, ValueError, OSError):
        # A closed or replaced stream (pytest's capture, a service manager)
        # is not a person either.
        return False


def _ask(root: Path) -> Optional[str]:
    """Ask whether to branch off, and under what name. ``None`` = stay put."""
    suggestion = default_name()
    try:
        answer = input(
            f"create a git worktree for this launch, so it does not share "
            f"{root.name} with other agents? [y/N]: "
        ).strip().lower()
    except EOFError:
        return None
    if answer not in ("y", "yes"):
        return None
    while True:
        try:
            raw = input(f"worktree name [{suggestion}]: ").strip()
        except EOFError:
            return suggestion
        if not raw:
            return suggestion
        try:
            return validate_name(raw)
        except WorktreeError as exc:
            print(f"  {exc}", file=sys.stderr)


def resolve(cwd: str, choice: Choice, *, resuming: bool = False) -> Optional[Worktree]:
    """Turn a ``--worktree`` answer -- and maybe a question -- into a checkout.

    Returns the worktree to launch in, or ``None`` to stay in ``cwd``. Raises
    only when a worktree was *asked for* and could not be made: an unanswered
    question never fails a launch.

    ``resuming`` says the launch opens an existing conversation
    (``--resume``, ``--continue``, ``--session-id``). Claude Code keeps
    transcripts **per working directory**, so a conversation resumed in a
    checkout that has never been worked in resolves to nothing -- bare
    ``--resume`` opens an empty picker and ``--resume <uuid>`` finds no such
    conversation. A *new* worktree and a resume are therefore contradictory,
    and the question is not even worth asking: ``claunch run nc --resume``
    means "carry on where I was", which is this directory.

    An *existing* worktree is a different matter and stays allowed. It has a
    history of its own, so ``--worktree=review --resume`` is the useful thing
    it looks like: go back to that checkout and carry on the work done there.
    """
    if choice is NEVER or choice is False:
        return None
    explicit = choice is not ASK
    root = repo_root(cwd)
    if root is None:
        if explicit:
            raise WorktreeError(
                f"{cwd} is not inside a git repository, so there is nothing "
                "to make a worktree of"
            )
        return None
    if explicit:
        name = validate_name(str(choice)) if choice else default_name()
        if resuming and not (worktrees_dir(root) / name).exists():
            raise WorktreeError(
                f"worktree {name!r} does not exist yet, and a resumed "
                "conversation cannot be opened in a new one: claude keeps "
                "transcripts per working directory, so a fresh checkout has "
                "none to resume. Name a worktree that already exists, or drop "
                "the resume and start a conversation there"
            )
        return create(root, name)
    if resuming:
        # Nothing to ask: the answer that a resume implies is "here".
        return None
    if not interactive():
        return None
    chosen = _ask(root)
    return create(root, chosen) if chosen else None


def announce(wt: Optional[Worktree], stream=None) -> None:
    """Report where the launch is going, once, on stderr."""
    if wt is None:
        return
    verb = "created" if wt.created else "reusing"
    print(
        f"{verb} worktree {wt.name!r} on branch {wt.branch!r}: {wt.path}",
        file=stream or sys.stderr,
    )
