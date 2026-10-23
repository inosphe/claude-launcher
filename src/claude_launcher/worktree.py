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

One thing does not come along with the checkout, and :func:`inherit_mcp_approval`
carries it: which ``.mcp.json`` servers the person has approved. See that
function for why the omission stops an agent dead.

**A repository may make its own checkouts.** ``git worktree add`` checks out
tracked files and nothing else, which for an Unreal project means a checkout
with no ``Intermediate/``, ``DerivedDataCache/`` or ``Binaries/`` -- hours of
rebuild before the agent can compile anything. ``.claunch/worktree.yaml`` can
name a command that creates the checkout instead (a copy-on-write clone on a
ReFS volume, say); see :func:`load_create_hook`. Whether a new checkout will
be such a clone is decided *before* it is made and shown to whoever is about
to make one (:func:`creation_mode`), and recorded on the checkout afterwards
(:func:`created_mode`), because "is this a full copy of my 200 GB tree or a
bare checkout" is not something to find out from the disk meter.
"""

from __future__ import annotations

import json
import os
import re
import subprocess
import sys
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional, Tuple, Union

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


#: Claude Code's per-directory settings file, relative to a checkout's root.
#: Its ``enabledMcpjsonServers`` is where the answer to "New MCP server found
#: in this project" is recorded -- see :func:`inherit_mcp_approval`.
LOCAL_SETTINGS = Path(".claude") / "settings.local.json"

#: The one key a new worktree inherits from the checkout it was cut from.
MCP_APPROVAL_KEY = "enabledMcpjsonServers"

#: A repository's own worktree settings, relative to its root.
CREATE_CONFIG = Path(".claunch") / "worktree.yaml"

#: Written into a checkout's git admin directory (``<common>/worktrees/<id>``)
#: by :func:`create`: how the checkout was made. There and not in the tree,
#: so it is never an untracked file and goes away with ``git worktree prune``.
CREATE_RECORD = "claunch-create.json"

#: Seconds a create hook may run when the config does not say. A block clone
#: of a large Unreal tree takes about a minute; a hook that falls back to a
#: full copy can take far longer, and is still better finished than killed.
DEFAULT_CREATE_TIMEOUT = 1800.0

#: Filesystems on which a Windows block clone (FSCTL_DUPLICATE_EXTENTS_TO_FILE)
#: is possible. NTFS is not one of them: a "copy-on-write" hook run there copies
#: every byte.
BLOCK_CLONE_FILESYSTEMS = frozenset({"ReFS"})

#: How a new checkout will be made, as :func:`creation_mode` reports it.
MODE_GIT = "git"                    # git worktree add: tracked files only
MODE_HOOK = "hook"                  # the repository's own command
MODE_COW = "cow"                    # that command, block-cloning on ReFS
MODE_COW_UNAVAILABLE = "cow-unavailable"  # declared cow, but cannot clone here
MODE_INVALID = "invalid"            # the config is broken; creating fails


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
    #: The branch it was rebased onto on the way in, if it was asked for.
    #: Only ever set for a *reused* checkout: a fresh one is cut from the
    #: base and has nothing to catch up on.
    rebased: str = ""
    #: What a *new* checkout was asked to branch from (``branch_from``), as
    #: the caller named it -- a branch, a commit, or another worktree. Only
    #: ever set for a fresh checkout, so it and ``rebased`` never both hold.
    branched_from: str = ""
    #: How a *fresh* checkout was made (one of the ``MODE_*`` values); for a
    #: reused one, what its creation record says, or ``""`` when it has none.
    create_mode: str = ""


@dataclass(frozen=True)
class CreateHook:
    """A repository's own command for making a checkout (``create:``)."""

    #: argv, run with the repository root as the working directory.
    command: List[str] = field(default_factory=list)
    timeout: float = DEFAULT_CREATE_TIMEOUT
    #: The repository says the command block-clones. Only a claim: whether the
    #: volume can honour it is :func:`creation_mode`'s question.
    cow: bool = False


# --------------------------------------------------------------------------- #
# git
# --------------------------------------------------------------------------- #
#: Seconds a *read-only* git (branch, root, listing) may take before it is
#: given up on. Generous for the answer — these are local, index-only reads
#: that finish in milliseconds — and finite for the case that matters: a git
#: that wedges (a lock held by something else, a checkout on a filesystem
#: that stopped answering) must not become a caller that wedges with it.
_READ_TIMEOUT = 10.0


def _git(
    args: List[str], *, cwd: str, timeout: Optional[float] = None
) -> subprocess.CompletedProcess:
    """Run git in ``cwd`` and hand back the finished process.

    ``timeout`` bounds the wait for callers that are only *reading* — a
    reading that never returns is worse than one that fails, because the
    caller has nothing to fall back on and nowhere to give up. A git that
    outstays it is killed and reported as a failure (returncode 1, the
    message on stderr), which is the same shape every caller here already
    handles for a git that simply said no. Left as ``None`` for the callers
    that change something: cutting a worktree or rebasing one is allowed to
    take as long as it takes, and killing it half-done would leave the
    checkout in a state nobody asked for.
    """
    try:
        return subprocess.run(
            ["git", *args],
            cwd=cwd,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            stdin=subprocess.DEVNULL,
        )
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(
            ["git", *args],
            1,
            "",
            f"git {' '.join(args)} did not finish within {timeout}s",
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


def pane_label(identity: str, cwd: str, role: str = "") -> str:
    """What a launch is called on a pane: who (as which role), on which
    branch, in which directory.

    Read from the directory the launch actually lands in rather than from a
    worktree that was just made, so the three facts are the same three
    whether the checkout was cut a moment ago, entered from an earlier run,
    or never a worktree at all. The path is what separates two checkouts of
    one branch, which is the case a fleet runs into first. The role is the
    session's own (``SessionDef.role``); a launch without one labels exactly
    as before.
    """
    branch = current_branch(Path(cwd)) if cwd and os.path.isdir(cwd) else ""
    return herdr.launch_label(identity, branch, cwd, role)


def current_branch(cwd: Path) -> str:
    """Branch checked out in ``cwd``; empty on a detached HEAD or on error."""
    done = _git(
        ["rev-parse", "--abbrev-ref", "HEAD"],
        cwd=str(cwd),
        timeout=_READ_TIMEOUT,
    )
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


def default_name(now: Optional[datetime] = None, *, token: Optional[str] = None) -> str:
    """The name used when a human wants a worktree but not to name one.

    Pane plus timestamp: unique per pane per second, and legible afterwards --
    ``git worktree list`` then says which pane made each checkout, and when.

    ``token`` replaces the pane: the daemon passes the session the checkout
    is for, because its own environment names no pane of the asker's -- it
    named whichever pane had started the daemon (claunch-2yiij.3).
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    head = herdr.sanitize_fragment(token or "") if token is not None else ""
    return f"{head or pane_token()}-{stamp}"


def child_name(parent: str, child: str, now: Optional[datetime] = None) -> str:
    """The name of a worktree cut FOR a spawned child, when nobody names one.

    ``<parent>-<child>-<stamp>``: the session that asked for the checkout, the
    session that gets it, and the second it was cut.
    :func:`default_name` answers the same question for a human at a Herdr
    pane and can only name the pane; a child has no pane, and the browser
    that opens the spawn form does not yet know the child's name either --
    so what a fleet used to leave behind was a repository full of
    ``s45-<stamp>`` checkouts, every one of them named after the *parent*
    that dispatched them and none of them saying which worker's it was.

    The stamp stays even though a live session name is already unique here:
    ``clear-sessions --logs`` frees the numbers again while the checkouts and
    branches they cut stay on disk, so a recycled ``s45-s110`` would be
    RESOLVED to the previous child's checkout (see :func:`resolve`) and the
    new worker would wake up on someone else's branch.
    """
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    parts = [_fragment(parent), _fragment(child), stamp]
    return "-".join(p for p in parts if p)


def _fragment(text: str) -> str:
    """``text`` reduced to what :func:`validate_name` accepts, or ``""``."""
    return re.sub(r"[^A-Za-z0-9._]+", "-", str(text or "")).strip("-.")


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


def exclude_pattern(root: Path, directory: Path) -> Optional[str]:
    """The ``info/exclude`` line that hides ``directory``, or ``None``.

    Anchored to the repository root (leading ``/``) and written with forward
    slashes on every platform, since git patterns never use backslashes.
    ``None`` when the directory is not inside ``root``: there is no
    root-relative pattern for it, and it cannot show up as untracked anyway.
    """
    for base, target in ((root, directory), (root.resolve(), directory.resolve())):
        try:
            rel = Path(os.path.abspath(target)).relative_to(os.path.abspath(base))
        except ValueError:
            continue
        text = rel.as_posix().strip("/")
        return f"/{text}/" if text and text != "." else None
    return None


def hide_worktrees(root: Path) -> bool:
    """Add the worktrees directory to the repository's ``info/exclude``.

    A launcher worktree is an untracked directory of the main checkout, so
    ``git ls-files --others --exclude-standard`` lists it and
    ``daemon.runtime_state._dirty_paths`` counts it as code in no commit
    (claunch-6pcyo). ``info/exclude`` is repository-local and untracked: the
    user's ``.gitignore`` and tracked files stay as they are. Appending is
    idempotent, and a directory that already holds worktrees is covered too
    because this runs on every :func:`create`, including the reuse path.

    Best effort: returns whether the line is present afterwards, and never
    raises, because failing to hide a directory must not stop a launch.
    """
    pattern = exclude_pattern(root, worktrees_dir(root))
    if pattern is None:
        return False
    try:
        done = _git(
            ["rev-parse", "--git-path", "info/exclude"],
            cwd=str(root),
            timeout=_READ_TIMEOUT,
        )
        out = (done.stdout or "").strip()
        if done.returncode != 0 or not out:
            return False
        target = Path(out)
        if not target.is_absolute():
            target = root / target
        try:
            text = target.read_text(encoding="utf-8")
        except FileNotFoundError:
            text = ""
        if pattern in (line.strip() for line in text.splitlines()):
            return True
        target.parent.mkdir(parents=True, exist_ok=True)
        with open(target, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(("\n" if text and not text.endswith("\n") else "") + pattern + "\n")
        return True
    except (OSError, WorktreeError):
        return False


def load_create_hook(root: Path) -> Tuple[Optional[CreateHook], str]:
    """The repository's create hook, and why it could not be read.

    ``(None, "")`` when ``.claunch/worktree.yaml`` is absent or has no
    ``create:`` -- the ordinary case, and ``git worktree add`` makes the
    checkout. ``(None, reason)`` when the file is there but unusable: a
    repository that asked for its own command must not silently get a bare
    checkout instead, so :func:`create` refuses on it and the pickers say so.

    The file::

        create:
          command: [pwsh, -NoProfile, -File, tools/cow-worktree.ps1]
          timeout: 1800   # seconds, optional
          cow: true       # the command block-clones; optional

    ``command`` is an argv list, run from the repository root. It must leave a
    worktree of this repository registered at ``$CLAUNCH_WT_PATH`` on the
    branch ``$CLAUNCH_WT_BRANCH`` (see :func:`_run_create_hook`).
    """
    path = root / CREATE_CONFIG
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None, ""
    except OSError as exc:
        return None, f"cannot read {CREATE_CONFIG.as_posix()}: {exc}"
    import yaml

    try:
        data = yaml.safe_load(text) or {}
    except yaml.YAMLError as exc:
        return None, f"{CREATE_CONFIG.as_posix()} is not valid YAML: {exc}"
    if not isinstance(data, dict):
        return None, f"{CREATE_CONFIG.as_posix()} must be a mapping"
    spec = data.get("create")
    if spec is None:
        return None, ""
    where = f"{CREATE_CONFIG.as_posix()}: create"
    if not isinstance(spec, dict):
        return None, f"{where} must be a mapping"
    command = spec.get("command")
    if (not isinstance(command, list) or not command
            or not all(isinstance(a, str) and a for a in command)):
        return None, f"{where}.command must be a non-empty list of strings"
    timeout = spec.get("timeout", DEFAULT_CREATE_TIMEOUT)
    if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) \
            or timeout <= 0:
        return None, f"{where}.timeout must be a positive number of seconds"
    cow = spec.get("cow", False)
    if not isinstance(cow, bool):
        return None, f"{where}.cow must be true or false"
    return CreateHook(command=list(command), timeout=float(timeout), cow=cow), ""


def _existing_ancestor(path: Path) -> Path:
    """``path`` or its nearest ancestor that exists (the worktrees directory
    is created on the first checkout, and the volume is asked before that)."""
    current = Path(os.path.abspath(path))
    while not current.exists() and current.parent != current:
        current = current.parent
    return current


def _volume_root(path: Path) -> str:
    """The mount point ``path`` lives on (``F:\\``, or a mounted folder);
    ``""`` off Windows or when Windows will not say."""
    if os.name != "nt":
        return ""
    import ctypes

    buf = ctypes.create_unicode_buffer(1024)
    ok = ctypes.windll.kernel32.GetVolumePathNameW(
        str(_existing_ancestor(path)), buf, len(buf)
    )
    return buf.value if ok else ""


def volume_filesystem(path: Path) -> str:
    """The filesystem name of the volume ``path`` is on (``ReFS``, ``NTFS``).

    ``""`` when it cannot be read -- including on every platform but Windows,
    where block cloning means something else and no hook claims it today.
    """
    volume = _volume_root(path)
    if not volume:
        return ""
    import ctypes

    name = ctypes.create_unicode_buffer(261)
    ok = ctypes.windll.kernel32.GetVolumeInformationW(
        volume, None, 0, None, None, None, name, len(name)
    )
    return name.value if ok else ""


def _same_volume(a: Path, b: Path) -> Optional[bool]:
    """Whether ``a`` and ``b`` are on one volume; ``None`` when unknown.
    A block clone cannot cross volumes, whatever both of them are."""
    va, vb = _volume_root(a), _volume_root(b)
    if not va or not vb:
        return None
    return os.path.normcase(va) == os.path.normcase(vb)


def creation_mode(root: Path) -> dict:
    """How a *new* worktree of ``root`` would be made, decided before it is.

    The answer every picker shows next to "new worktree", so the person
    creating one knows whether they are about to get a block-cloned copy of
    the whole tree (build caches and all), a bare ``git worktree add``, or --
    the case worth a warning -- a hook that claims copy-on-write on a volume
    that cannot do it and will therefore copy every byte.

    ``mode`` is one of the ``MODE_*`` values; ``summary`` is one line for a
    person, written to follow the mode's name in :data:`MODE_WORDS`
    ("copy-on-write: block-cloned on ReFS by ..."); the rest are the facts it
    was decided from.
    """
    hook, error = load_create_hook(root)
    target = worktrees_dir(root)
    filesystem = volume_filesystem(target)
    same = _same_volume(root, target)
    block_clone = filesystem in BLOCK_CLONE_FILESYSTEMS and same is not False
    command = " ".join(hook.command) if hook else ""
    if error:
        mode = MODE_INVALID
        summary = f"{error} -- creating a worktree will fail until it is fixed"
    elif hook is None:
        mode = MODE_GIT
        summary = ("tracked files only (no create hook in "
                   f"{CREATE_CONFIG.as_posix()})")
    elif not hook.cow:
        mode = MODE_HOOK
        summary = f"made by {command}"
    elif block_clone:
        mode = MODE_COW
        summary = f"block-cloned on {filesystem} by {command}"
    else:
        mode = MODE_COW_UNAVAILABLE
        if same is False:
            why = "the worktrees directory is on a different volume from the repository"
        else:
            why = f"{target.anchor or 'the volume'} is {filesystem or 'not a known filesystem'}"
        summary = (f"the create hook is declared copy-on-write, but {why}: "
                   "block cloning is unavailable, so every file will be copied")
    return {
        "mode": mode,
        "summary": summary,
        "cow": mode == MODE_COW,
        "command": list(hook.command) if hook else [],
        "config": (root / CREATE_CONFIG).as_posix() if (hook or error) else "",
        "filesystem": filesystem,
        "same_volume": same,
        "block_clone": block_clone,
        "error": error,
    }


def _admin_dir(path: Path) -> Optional[Path]:
    """A linked checkout's git admin directory, read from its ``.git`` file.

    Read rather than asked of git, because the Worktrees page reads it for
    every row of every repository and a git process per row is what that
    page is careful not to spend.
    """
    try:
        text = (Path(path) / ".git").read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not text.startswith("gitdir:"):
        return None
    admin = Path(text[len("gitdir:"):].strip())
    return admin if admin.is_absolute() else Path(path) / admin


def _record_creation(path: Path, mode: str, hook: Optional[CreateHook]) -> None:
    """Note on the checkout how it was made. Best effort: a record that
    cannot be written costs a badge, never a launch."""
    admin = _admin_dir(path)
    if admin is None:
        return
    try:
        (admin / CREATE_RECORD).write_text(json.dumps({
            "mode": mode,
            "command": list(hook.command) if hook else [],
            "created_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        }), encoding="utf-8")
    except OSError:
        pass


def created_mode(path: Path) -> str:
    """How the checkout at ``path`` was made, from its creation record.

    ``""`` for a checkout claunch did not make, or made before the record
    existed -- unknown, which is not the same as ``git``.
    """
    admin = _admin_dir(path)
    if admin is None:
        return ""
    try:
        data = json.loads((admin / CREATE_RECORD).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return ""
    mode = data.get("mode") if isinstance(data, dict) else None
    return mode if isinstance(mode, str) else ""


def _run_create_hook(
    root: Path, hook: CreateHook, path: Path, name: str, *,
    new_branch: bool, start: str,
) -> None:
    """Run the repository's create command and hold it to the contract.

    The command gets the request in its environment -- ``CLAUNCH_WT_ROOT``
    (the main checkout), ``CLAUNCH_WT_PATH`` (where the checkout must be),
    ``CLAUNCH_WT_BRANCH``, ``CLAUNCH_WT_NEW_BRANCH`` (``1`` to cut that branch,
    ``0`` to check out the existing one) and ``CLAUNCH_WT_START`` (where a new
    branch starts; empty for HEAD). Exiting zero is not enough: the checkout
    must then be a registered worktree of this repository, or the launch would
    land in a directory git does not know.
    """
    env = dict(os.environ)
    env.update({
        "CLAUNCH_WT_ROOT": str(root),
        "CLAUNCH_WT_PATH": str(path),
        "CLAUNCH_WT_BRANCH": name,
        "CLAUNCH_WT_NEW_BRANCH": "1" if new_branch else "0",
        "CLAUNCH_WT_START": start,
    })
    shown = " ".join(hook.command)
    try:
        done = subprocess.run(
            hook.command, cwd=str(root), env=env, capture_output=True,
            text=True, encoding="utf-8", errors="replace",
            stdin=subprocess.DEVNULL, timeout=hook.timeout,
        )
    except FileNotFoundError as exc:
        raise WorktreeError(f"create hook {shown!r} could not start: {exc}")
    except subprocess.TimeoutExpired:
        raise WorktreeError(
            f"create hook {shown!r} did not finish in {hook.timeout:g}s"
        )
    if done.returncode != 0:
        tail = "\n".join((done.stderr or done.stdout or "").strip().splitlines()[-15:])
        raise WorktreeError(
            f"create hook {shown!r} exited {done.returncode}" +
            (f":\n{tail}" if tail else "")
        )
    if not path.exists() or not _registered(root, path):
        raise WorktreeError(
            f"create hook {shown!r} exited 0 but {path} is not a registered "
            "worktree of this repository"
        )


def create(root: Path, name: str, *, start: str = "") -> Worktree:
    """Create -- or reuse -- the worktree ``name`` of the repository at ``root``.

    Reuse is deliberate: ``--worktree=review`` a second time should return to
    that checkout with its branch and its uncommitted work intact, not fail
    because the directory is already there.

    ``start`` is where a *new* branch is cut from; empty means the
    repository's HEAD. ``root`` is the main checkout (see :func:`repo_root`),
    so HEAD is usually the trunk -- a caller that wants the branch to begin
    on its own branch, the way a stacked pull request begins on the one
    below it, names that branch here. Ignored when the branch already
    exists: a branch that has history is checked out as it stands.

    A new checkout is made by the repository's create hook when it has one
    (:func:`load_create_hook`) and by ``git worktree add`` otherwise; either
    way the checkout is stamped with how (:func:`created_mode`).
    """
    name = validate_name(name)
    path = worktrees_dir(root) / name
    hide_worktrees(root)
    if path.exists():
        if not _registered(root, path):
            raise WorktreeError(
                f"{path} already exists and is not a worktree of this repository"
            )
        return Worktree(
            path=path, name=name, branch=current_branch(path) or name, created=False,
            create_mode=created_mode(path),
        )
    hook, error = load_create_hook(root)
    if error:
        raise WorktreeError(error)
    path.parent.mkdir(parents=True, exist_ok=True)
    # An existing branch is checked out as it stands (resuming named work); a
    # new one is cut from HEAD, or from `start` when the caller named one.
    # `git worktree add` refuses either if the branch is checked out somewhere
    # else, which is the refusal we want.
    reused_branch = _branch_exists(root, name)
    if hook is not None:
        mode = creation_mode(root)["mode"]
        _run_create_hook(root, hook, path, name,
                         new_branch=not reused_branch, start=start)
    else:
        mode = MODE_GIT
        args = (
            ["worktree", "add", str(path), name]
            if reused_branch
            else ["worktree", "add", "-b", name, str(path), *([start] if start else [])]
        )
        done = _git(args, cwd=str(root))
        if done.returncode != 0:
            # git writes its progress line ("Preparing worktree ...") to
            # stderr ahead of the refusal, and some failures leave stdout as
            # the only place with a reason, so keep both and the exit status.
            parts = [(done.stderr or "").strip(), (done.stdout or "").strip()]
            detail = "\n".join(p for p in parts if p)
            raise WorktreeError(
                f"git worktree add failed (exit {done.returncode}): {detail}"
            )
    _record_creation(path, mode, hook)
    inherit_mcp_approval(root, path)
    return Worktree(
        path=path, name=name, branch=current_branch(path) or name, created=True,
        create_mode=mode,
    )


def inherit_mcp_approval(root: Path, path: Path) -> None:
    """Carry ``root``'s approved ``.mcp.json`` servers into the new worktree.

    A project's MCP servers are declared in the repository's ``.mcp.json``,
    and Claude Code resolves that file from the repository -- so a worktree
    inherits the *declaration* without holding a copy. The *approval* does
    not travel with it: the answer to "New MCP server found in this project"
    is recorded per directory, in that directory's
    ``.claude/settings.local.json``. A fresh worktree is a fresh directory,
    so the question is asked again, every time.

    Asked of whom, is the problem. An agent launched into a worktree meets
    that modal on its first screen and cannot answer it -- the one question
    in the launch path with nobody able to reply. Carrying the answer over is
    not a new trust decision either; the person already made it in the
    checkout this worktree was cut from, for the very same ``.mcp.json``.

    So exactly the answer is copied, and only where there is one to copy:

    - the ``enabledMcpjsonServers`` list alone, never the rest of the file
      (permissions and env live there too, and inheriting those silently
      would leave nobody able to say what a worktree is running under);
    - never ``enableAllProjectMcpServers``, which answers for servers the
      person has not seen;
    - nothing at all when ``root`` has no approval recorded -- inventing one
      *would* be a new decision;
    - nothing when the worktree already has a settings file, which is where
      a reused checkout's own answers live.

    Failure here is not worth a failed launch: the worst case is the modal
    the person was already getting, so an unreadable or unwritable settings
    file leaves the worktree as ``git worktree add`` made it.
    """
    try:
        doc = json.loads((root / LOCAL_SETTINGS).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return
    approved = doc.get(MCP_APPROVAL_KEY) if isinstance(doc, dict) else None
    if not isinstance(approved, list):
        return
    names = [server for server in approved if isinstance(server, str) and server]
    if not names:
        return
    target = path / LOCAL_SETTINGS
    if target.exists():
        return
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(
            json.dumps({MCP_APPROVAL_KEY: names}, indent=2) + "\n",
            encoding="utf-8",
        )
    except OSError:
        return



# --------------------------------------------------------------------------- #
# reading a repository
# --------------------------------------------------------------------------- #
def branches(cwd: str) -> List[str]:
    """Local branch names of ``cwd``'s repository, most recently committed first.

    The order is the useful one for a picker: whatever the person has been
    working on is near the top, and ``master`` stays findable by typing 'm'.
    """
    root = repo_root(cwd)
    if root is None:
        return []
    done = _git(
        ["for-each-ref", "--format=%(refname:short)", "--sort=-committerdate",
         "refs/heads"],
        cwd=str(root),
    )
    if done.returncode != 0:
        return []
    return [line.strip() for line in (done.stdout or "").splitlines() if line.strip()]


def existing(cwd: str) -> List[str]:
    """Launcher worktrees of ``cwd``'s repository, by name.

    What the pickers offer beside "make a new one", because reuse is the
    common case a *name* exists for: naming the same worktree twice returns
    to that checkout with its branch and its work intact, and remembering
    which names are taken is exactly what a list is for.
    """
    root = repo_root(cwd)
    return [name for name, _ in _existing_paths(root)] if root else []


def _existing_paths(root: Path) -> List[tuple]:
    """``(name, absolute path)`` of every launcher worktree of ``root``.

    The path is what lets a picker say which *session* sits in a checkout:
    a session record carries its directory, not the worktree name, so the
    two are joined on the path.
    """
    base = worktrees_dir(root)
    done = _git(["worktree", "list", "--porcelain"], cwd=str(root))
    if done.returncode != 0:
        return []
    pairs: List[tuple] = []
    for line in (done.stdout or "").splitlines():
        if not line.startswith("worktree "):
            continue
        path = os.path.abspath(line[len("worktree "):].strip())
        try:
            rel = os.path.relpath(path, str(base))
        except ValueError:  # a different drive on Windows
            continue
        if rel.startswith("..") or rel == ".":
            continue
        pairs.append((rel.replace(os.sep, "/"), path))
    return pairs


def _existing_branches(root: Path) -> dict:
    """``name -> branch`` of every launcher worktree of ``root`` ("" when
    detached), read off the same porcelain listing as :func:`_existing_paths`
    -- one git process however many checkouts there are."""
    base = worktrees_dir(root)
    done = _git(["worktree", "list", "--porcelain"], cwd=str(root))
    if done.returncode != 0:
        return {}
    out: dict = {}
    name = None
    for line in (done.stdout or "").splitlines():
        if line.startswith("worktree "):
            path = os.path.abspath(line[len("worktree "):].strip())
            try:
                rel = os.path.relpath(path, str(base))
            except ValueError:  # a different drive on Windows
                rel = ".."
            name = (None if rel.startswith("..") or rel == "."
                    else rel.replace(os.sep, "/"))
            if name is not None:
                out[name] = ""
        elif line.startswith("branch refs/heads/") and name is not None:
            out[name] = line[len("branch refs/heads/"):].strip()
    return out


def info(cwd: str) -> dict:
    """Everything a worktree picker needs about ``cwd``, in one answer.

    One call because it is one round trip when the asker is not on this
    machine: the web UI and the ``spawn`` form are describing the *daemon's*
    filesystem, and three endpoints for "is it a repo, what is it on, what is
    beside it" would be three chances to answer from three different states.
    """
    root = repo_root(cwd)
    if root is None:
        return {"repo": False, "root": "", "branch": "", "branches": [],
                "worktrees": [], "paths": {}, "worktree_branches": {},
                "root_branch": "", "create": None}
    pairs = _existing_paths(root)
    return {
        "repo": True,
        "root": str(root),
        "branch": current_branch(Path(cwd)),
        # What the main checkout is on: where a new worktree starts when no
        # branch_from is given (create() cuts from the root's HEAD), which is
        # not always the trunk and not always ``branch`` above.
        "root_branch": current_branch(root),
        "branches": branches(cwd),
        "worktrees": [name for name, _ in pairs],
        # name -> absolute path, so a picker can join a session's cwd to the
        # checkout it sits in ("reuse s123's worktree").
        "paths": dict(pairs),
        # name -> the branch checked out there ("" when detached), so a
        # "branch from" picker can say what a new checkout would start on.
        "worktree_branches": _existing_branches(root),
        # How a new checkout would be made -- copy-on-write or a bare
        # `git worktree add` -- so the picker can say so before it is made.
        "create": creation_mode(root),
    }


def is_dirty(path: Path) -> bool:
    """Whether ``path`` has uncommitted changes to *tracked* files.

    Untracked files are not counted, because a rebase does not care about
    them -- and refusing a launch over a stray build artefact would be a
    refusal nobody could act on.
    """
    done = _git(["status", "--porcelain", "--untracked-files=no"], cwd=str(path))
    if done.returncode != 0:
        return False
    return bool((done.stdout or "").strip())


def rebase(wt: "Worktree", base: str) -> "Worktree":
    """Bring a reused checkout up to date with ``base``, or refuse the launch.

    Reuse is the whole point of naming a worktree, and a checkout you come
    back to a day later is a day behind whatever it was cut from. So the
    reused case gets the offer the fresh case does not need: rebase onto the
    branch this one is a branch *of* -- ``master`` for a launch from the main
    checkout, the parent's own branch for a child.

    **A failure here fails the launch, and leaves nothing behind.** An agent
    started in a half-rebased checkout is worse than one that never started:
    the conflict is not its work, the branch is not in a state it can commit
    from, and it would spend its first turn on a mess it did not make and
    cannot explain. Uncommitted work is refused *before* anything is run
    (there is no safe automatic answer to it), and a conflict aborts the
    rebase before it raises, so "nothing happened" is literally true and the
    checkout is exactly as it was found.

    Local by design: no fetch, no pull. The base is a branch of this
    repository as it stands on this machine, which is what "rebase onto
    master" means when you type it yourself.
    """
    if not base:
        return wt
    if is_dirty(wt.path):
        raise WorktreeError(
            f"worktree {wt.name!r} has uncommitted changes, so it cannot be "
            f"rebased onto {base!r} -- commit or stash them in {wt.path} and "
            "launch again (or launch without the update)"
        )
    if _git(["rev-parse", "--verify", "--quiet", base], cwd=str(wt.path)).returncode != 0:
        raise WorktreeError(
            f"no branch {base!r} in this repository to rebase {wt.name!r} onto"
        )
    done = _git(["rebase", base], cwd=str(wt.path))
    if done.returncode != 0:
        # Back out of the rebase git left half-applied, so the refusal really
        # does leave the checkout untouched.
        _git(["rebase", "--abort"], cwd=str(wt.path))
        detail = (done.stderr or done.stdout or "").strip().splitlines()
        raise WorktreeError(
            f"rebasing worktree {wt.name!r} onto {base!r} failed, so nothing "
            f"was started (the rebase was aborted and {wt.path} is as it was): "
            + (detail[-1] if detail else "git rebase reported no reason")
        )
    return Worktree(
        path=wt.path, name=wt.name, branch=current_branch(wt.path) or wt.branch,
        created=wt.created, rebased=base, create_mode=wt.create_mode,
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
        # ``sys.stdin is None`` is checked here rather than left to the
        # AttributeError below: that except exists for closed or replaced
        # streams, and a missing stdin is a different fact about the process.
        return bool(
            sys.stdin is not None
            and sys.stdin.isatty()
            and sys.stderr.isatty()
        )
    except (AttributeError, ValueError, OSError):
        # A closed or replaced stream (pytest's capture, a service manager)
        # is not a person either.
        return False


def _ask(root: Path) -> Optional[str]:
    """Ask whether to branch off, and under what name. ``None`` = stay put."""
    suggestion = default_name()
    how = creation_mode(root)
    print(f"new worktrees of {root.name}: "
          f"{MODE_WORDS.get(how['mode'], how['mode'])}: {how['summary']}",
          file=sys.stderr)
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


def resolve(
    cwd: str,
    choice: Choice,
    *,
    resuming: bool = False,
    rebase_onto: str = "",
    branch_from: str = "",
) -> Optional[Worktree]:
    """Turn a ``--worktree`` answer -- and maybe a question -- into a checkout.

    Returns the worktree to launch in, or ``None`` to stay in ``cwd``. Raises
    only when a worktree was *asked for* and could not be made: an unanswered
    question never fails a launch.

    ``rebase_onto`` puts the checkout ON that branch: a *reused* one is
    rebased onto it before the agent is let into it (see :func:`rebase`),
    and a *new* one is cut from it instead of from HEAD -- so a nested
    worker's branch can begin on its parent's branch rather than on the
    trunk, the way a stacked pull request begins on the one below it. A new
    branch has nothing to replay, so no rebase runs and ``rebased`` stays
    empty. A rebase that cannot be done cleanly fails the launch, which is
    the same rule a worktree that cannot be made already follows.

    ``branch_from`` is the one-meaning spelling of the second half: where a
    *new* checkout starts -- a branch, a commit, or the name of another
    worktree (which starts it on whatever that worktree has checked out; see
    :func:`start_point`). It refuses a name that already exists, because an
    existing checkout keeps the branch it has and would silently ignore the
    answer, and it refuses ``rebase_onto`` beside it: one launch either
    starts a new branch somewhere or brings a reused one up to date, and the
    two fields are those two questions (see :func:`_checkout`).

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
        return _checkout(root, name, rebase_onto=rebase_onto, branch_from=branch_from)
    if resuming:
        # Nothing to ask: the answer that a resume implies is "here".
        return None
    if not interactive():
        return None
    chosen = _ask(root)
    return (
        _checkout(root, chosen, rebase_onto=rebase_onto, branch_from=branch_from)
        if chosen
        else None
    )


def start_point(root: Path, ref: str) -> str:
    """What ``branch_from`` names, in the spelling ``git worktree add`` takes.

    A launcher worktree's name is read as that checkout: its branch, or its
    commit when it is on a detached HEAD. Anything else has to be a branch
    or commit of this repository. A worktree wins over a branch of the same
    name, because naming a checkout is what the picker offers -- and its
    branch is normally that same name anyway (:func:`create`).

    Only what is *committed* there is carried: uncommitted changes in the
    other checkout stay where they are.
    """
    ref = (ref or "").strip()
    try:
        path = worktrees_dir(root) / validate_name(ref)
    except WorktreeError:
        path = None
    if path is not None and path.exists() and _registered(root, path):
        branch = current_branch(path)
        if branch:
            return branch
        done = _git(["rev-parse", "HEAD"], cwd=str(path), timeout=_READ_TIMEOUT)
        if done.returncode == 0 and (done.stdout or "").strip():
            return (done.stdout or "").strip()
    if _git(
        ["rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}"], cwd=str(root)
    ).returncode != 0:
        raise WorktreeError(
            f"nothing named {ref!r} to branch from: it is no worktree, branch "
            "or commit of this repository"
        )
    return ref


def _checkout(
    root: Path, name: str, *, rebase_onto: str = "", branch_from: str = ""
) -> Worktree:
    """Make or reuse the worktree ``name``, answering at most one of the two
    branch questions.

    | field         | new worktree                 | existing worktree        |
    |---------------|------------------------------|--------------------------|
    | ``branch_from`` | starts the branch there    | refused (it has a branch)|
    | ``rebase_onto`` | starts the branch there (*) | rebased onto it         |

    (*) kept for the callers that already spell a stacked child's start this
    way (``spawn`` with ``rebase_onto: <parent>-stack``). Both fields at once
    is refused rather than ranked: the pair asks two different things of one
    checkout, and whichever was dropped would be dropped without a word.
    """
    if not branch_from:
        return _updated(create(root, name, start=rebase_onto), rebase_onto)
    if rebase_onto:
        raise WorktreeError(
            f"branch_from ({branch_from!r}) and rebase_onto ({rebase_onto!r}) "
            "were both given: branch_from is where a NEW worktree starts, "
            "rebase_onto brings an EXISTING one up to date -- send one"
        )
    name = validate_name(name)
    if (worktrees_dir(root) / name).exists() or _branch_exists(root, name):
        raise WorktreeError(
            f"worktree or branch {name!r} already exists, so it cannot be "
            f"branched from {branch_from!r}: an existing checkout keeps its "
            "own branch. Pick a new name, or reuse it and use rebase_onto to "
            "bring it up to date"
        )
    wt = create(root, name, start=start_point(root, branch_from))
    return Worktree(
        path=wt.path, name=wt.name, branch=wt.branch, created=wt.created,
        branched_from=branch_from, create_mode=wt.create_mode,
    )


def _updated(wt: Worktree, base: str) -> Worktree:
    """Rebase a reused checkout onto ``base``; a fresh one was cut from it."""
    return wt if wt.created or not base else rebase(wt, base)


def announce(wt: Optional[Worktree], stream=None) -> None:
    """Report where the launch is going, once, on stderr."""
    if wt is None:
        return
    verb = "created" if wt.created else "reusing"
    if wt.rebased:
        verb = f"reusing (rebased onto {wt.rebased})"
    if wt.branched_from:
        verb = f"created (branched from {wt.branched_from})"
    how = MODE_WORDS.get(wt.create_mode, "")
    print(
        f"{verb} worktree {wt.name!r} on branch {wt.branch!r}"
        f"{f' [{how}]' if how else ''}: {wt.path}",
        file=stream or sys.stderr,
    )


#: How :func:`announce` and the wizard name a creation mode. The cow-unavailable case says
#: what it cost, since that is the one a person did not mean to get.
MODE_WORDS = {
    MODE_GIT: "git worktree add",
    MODE_HOOK: "create hook",
    MODE_COW: "copy-on-write",
    MODE_COW_UNAVAILABLE: "full copy: copy-on-write unavailable",
    MODE_INVALID: "create config error",
}
