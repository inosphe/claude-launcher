"""Workspaces: the directories a session may be spawned in.

A *workspace* is a named directory the user has vouched for once, on this
machine. It exists because a working directory typed free-hand is the easiest
thing to get wrong in the whole spawn form — a typo, a stale path, the wrong
drive — and the failure surfaces late and unhelpfully, as a PTY that could not
spawn. Registering the directory moves that check to the moment the user knows
the answer, and turns the web UI's directory field from a text box into a
picker with no way to spell it wrong.

The registry lives in ``~/.claunch.yaml`` under ``workspaces``, name -> path::

    workspaces:
      claude-launcher: F:\\works\\claude-launcher
      hq: D:\\works\\hq

It is **machine-local by default**: absolute paths mean nothing on another
machine, so ``workspaces`` is deliberately absent from
:data:`claude_launcher.sync.DEFAULT_SECTIONS`. A fleet whose machines really do
share a layout can opt in by listing it in ``sync.sections``.

The CLI (``claunch new-session -c DIR``) still takes any directory: it is
typed by someone who is already standing in the filesystem, with a shell that
completes paths. The registry is what the *browser* offers, which has neither.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from . import store

#: A workspace name: the same alphabet as a session name, so it stays easy to
#: type, quote-free in a shell, and safe in a URL.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")

#: Fallback when a path yields no usable name of its own (a drive root, say).
_FALLBACK_NAME = "ws"


class WorkspaceError(Exception):
    """Raised for an unusable workspace name, path, or lookup."""


@dataclass(frozen=True)
class Workspace:
    name: str
    path: str

    def exists(self) -> bool:
        return Path(self.path).is_dir()

    def to_dict(self) -> dict:
        # ``exists`` is resolved per call rather than stored: a workspace on a
        # removable drive is legitimately absent half the time, and the UI
        # wants to say so rather than have the entry disappear.
        return {"name": self.name, "path": self.path, "exists": self.exists()}


def normalize_path(raw: str) -> str:
    """An absolute, symlink-resolved path — the form the registry stores.

    Resolving here (rather than at read time) is what makes ``.`` and a
    relative path usable on the command line while the stored entry stays
    meaningful from anywhere, including the daemon.
    """
    text = str(raw or "").strip().strip('"')
    if not text:
        raise WorkspaceError("a workspace needs a directory path")
    try:
        return str(Path(text).expanduser().resolve())
    except OSError as exc:
        raise WorkspaceError(f"unusable path {raw!r}: {exc}") from exc


def _same_path(a: str, b: str) -> bool:
    # normcase is the difference between one workspace and two on Windows,
    # where 'F:\Works' and 'f:\works' are the same directory.
    return os.path.normcase(a) == os.path.normcase(b)


def _section(doc: Optional[dict] = None) -> Dict[str, str]:
    doc = store.load() if doc is None else doc
    block = doc.get("workspaces")
    if not isinstance(block, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in block.items()
        if isinstance(v, (str, os.PathLike)) and str(v).strip()
    }


def list_all(doc: Optional[dict] = None) -> List[Workspace]:
    """Every registered workspace, name-sorted (the order the pickers show)."""
    return [Workspace(name=n, path=p) for n, p in sorted(_section(doc).items())]


def get(name: str, doc: Optional[dict] = None) -> Optional[Workspace]:
    path = _section(doc).get(str(name or "").strip())
    return Workspace(name=name, path=path) if path else None


def find(name_or_path: str, doc: Optional[dict] = None) -> Optional[Workspace]:
    """Look a workspace up by name, or by the directory it points at.

    Both spellings are accepted because both are what the user has in hand:
    the name is what ``ls`` printed, the path is what their shell completed.
    """
    token = str(name_or_path or "").strip()
    if not token:
        return None
    entries = _section(doc)
    if token in entries:
        return Workspace(name=token, path=entries[token])
    try:
        wanted = normalize_path(token)
    except WorkspaceError:
        return None
    for name, path in sorted(entries.items()):
        if _same_path(path, wanted):
            return Workspace(name=name, path=path)
    return None


def owning(path: str, doc: Optional[dict] = None) -> Optional[Workspace]:
    """The registered workspace ``path`` *lives in*, or ``None``.

    :func:`find` answers "is this directory registered", which is the right
    question when someone is choosing one. This answers "whose is it", which
    is the right question when a directory already exists and has to be
    accounted for -- and the two stopped being the same question when
    ``claunch run --worktree`` started putting sessions in
    ``<repo>/.claude/worktrees/<name>``.

    A worktree is not a second workspace: it is the same repository the user
    already vouched for, checked out again on another branch, at a path
    claunch chose *inside* that workspace. Attributing it to the enclosing
    entry is what keeps it from reading as a stray directory nobody approved.

    Deliberately read-only, and deliberately not wired into
    :func:`claude_launcher.spawn.resolve_workspace`: containment is how a
    directory is *described*, never how one is *chosen*. What an agent may
    name stays exactly the list the user registered, or ``--workspace`` would
    quietly become the free-text path that ``spawn.allow_cwd`` exists to gate.

    The longest match wins, so a workspace registered inside another one (a
    subproject of a monorepo) still claims its own sessions.
    """
    exact = find(path, doc)
    if exact is not None:
        return exact
    try:
        wanted = Path(normalize_path(path))
    except WorkspaceError:
        return None
    best: Optional[Workspace] = None
    for name, raw in sorted(_section(doc).items()):
        try:
            root = Path(normalize_path(raw))
        except WorkspaceError:
            continue
        if not _contains(root, wanted):
            continue
        if best is None or len(str(root)) > len(best.path):
            best = Workspace(name=name, path=str(root))
    return best


def _contains(root: Path, child: Path) -> bool:
    """Whether ``child`` is ``root`` or sits underneath it."""
    try:
        parts_root = [os.path.normcase(p) for p in root.parts]
        parts_child = [os.path.normcase(p) for p in child.parts]
    except (TypeError, ValueError):
        return False
    return (
        len(parts_child) >= len(parts_root)
        and parts_child[: len(parts_root)] == parts_root
    )


def subpath(workspace: Workspace, path: str) -> str:
    """``path`` relative to its workspace root; ``""`` when it is the root.

    What a session listing needs in order to say *where inside* a workspace a
    session is sitting -- the worktree's name, in the case this exists for --
    without printing the whole absolute path twice.
    """
    try:
        rel = Path(normalize_path(path)).relative_to(Path(normalize_path(workspace.path)))
    except (ValueError, WorkspaceError):
        return ""
    text = str(rel)
    return "" if text == "." else text.replace(os.sep, "/")


def derive_name(path: str, taken: Optional[Dict[str, str]] = None) -> str:
    """A name for ``path``: its directory name, made safe and made unique.

    The basename is what the user already calls the directory, so it is the
    name they will recognise in the picker. Anything the alphabet rejects
    becomes '-'; a collision with a *different* directory gets a numeric
    suffix rather than silently taking the name over.
    """
    base = Path(path).name or Path(path).drive.rstrip(":\\/ ") or _FALLBACK_NAME
    candidate = re.sub(r"[^A-Za-z0-9._-]+", "-", base).strip("-") or _FALLBACK_NAME
    entries = _section() if taken is None else taken
    if candidate not in entries:
        return candidate
    n = 2
    while f"{candidate}-{n}" in entries:
        n += 1
    return f"{candidate}-{n}"


def check_name(name: str) -> str:
    canon = str(name or "").strip()
    if not _NAME_RE.match(canon):
        raise WorkspaceError(
            f"invalid workspace name {name!r}: use letters, digits, '.', '_' or '-'"
        )
    return canon


def add(raw_path: str, name: Optional[str] = None) -> Workspace:
    """Register a directory, returning the resulting workspace.

    The directory must already exist: vouching for a path that is not there is
    exactly the mistake the registry is meant to catch, and catching it here
    means the picker can never offer a directory that will fail to spawn.

    Re-adding a directory already registered is a no-op that returns the
    existing entry, so the command is safe to repeat (and to script).
    """
    path = normalize_path(raw_path)
    if not Path(path).is_dir():
        raise WorkspaceError(
            f"{path} is not a directory"
            if Path(path).exists()
            else f"no such directory: {path}"
        )
    entries = _section()
    existing = find(path)
    if existing is not None and (name is None or name == existing.name):
        return existing
    chosen = check_name(name) if name else derive_name(path, entries)
    clash = entries.get(chosen)
    if clash is not None and not _same_path(clash, path):
        raise WorkspaceError(
            f"workspace {chosen!r} already points at {clash} — pick another "
            f"--name, or remove it first with 'claunch workspace rm {chosen}'"
        )

    def _mutate(doc: dict) -> None:
        block = doc.get("workspaces")
        if not isinstance(block, dict):
            block = {}
            doc["workspaces"] = block
        # A rename ('add PATH --name other' for a path already registered)
        # moves the entry rather than leaving the directory listed twice.
        if existing is not None and existing.name != chosen:
            block.pop(existing.name, None)
        block[chosen] = path

    store.update(_mutate)
    return Workspace(name=chosen, path=path)


def remove(name_or_path: str) -> Workspace:
    """Drop one workspace (by name or by path). The directory is untouched."""
    found = find(name_or_path)
    if found is None:
        known = ", ".join(w.name for w in list_all()) or "none registered"
        raise WorkspaceError(
            f"no workspace named {name_or_path!r} (known: {known})"
        )

    def _mutate(doc: dict) -> None:
        block = doc.get("workspaces")
        if isinstance(block, dict):
            block.pop(found.name, None)
            if not block:
                doc.pop("workspaces", None)

    store.update(_mutate)
    return found
