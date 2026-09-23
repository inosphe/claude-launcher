"""Which database a board is: one per workspace, named.

``claunch beads`` used to answer "where is the board" with one rule — the
repository root of the caller's directory, and ``<root>/.beads/beads.db``
under it. That rule has no way to say anything else, so three things were
true at once: a registered workspace that is not its own git repository
(``design-projection`` inside ``gds5``) had no board of its own, a workspace
with no ``.beads/`` at all had no board and its sessions were told so, and
every issue the dashboard filed went to the daemon's own directory because
that is what the create form defaults to. The fleet ended up with one board
holding every workspace's work.

So the board is now chosen by **workspace**, and a workspace's database is a
setting:

``<workspace path>/.beads/beads.db``
    The default, and the same file the old rule resolved — a workspace that
    is a git checkout keeps the board it already had, with its
    ``issues.jsonl`` tracked beside it.

``beads.boards.<name>`` in ``~/.claunch.yaml``
    An override, and it names the **database file itself** rather than a
    directory. A directory would have to be guessed at (``.beads/`` under it?
    which name inside?) and the guess would be silent; a path that must end
    in ``.db`` and must have an existing parent directory is checked before
    it is stored (:func:`check_path`).

:data:`DEFAULT_BOARD` is the board for a directory that belongs to no
registered workspace. Its database is pinned into the same setting on first
run (:func:`ensure_default`), to the board the daemon was already using, so
every issue filed before workspaces had boards of their own stays exactly
where it is and reads as that board's.

The registry is machine-local for the same reason ``workspaces`` is: the
values are absolute paths, which mean nothing on another machine. It is
therefore deliberately absent from
:data:`claude_launcher.sync.DEFAULT_SECTIONS`.

This module is path arithmetic and one config section. It never runs ``br``
and never creates a database: :mod:`claude_launcher.cli_beads` resolves a
directory to a :class:`BoardRef` (:func:`claude_launcher.cli_beads.resolve`)
and composes the ``br`` commands, including the ``init`` that brings a
workspace's first board into being.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Dict, List, Optional

from . import store, workspaces

#: The board a directory belongs to when no registered workspace claims it.
#: A reserved *name*, not a workspace: a board of this name is looked up in
#: this module's own section, so a workspace could carry the same name
#: without the two entries being the same row.
DEFAULT_BOARD = "claunch-default"

#: The directory ``br init`` lays a board out in, and the database's name
#: inside it. Spelled here as well as in :mod:`claude_launcher.cli_beads`
#: because this module composes the default path and that one composes the
#: commands; :mod:`claude_launcher.cli_beads` reads these two names from here
#: so there is still one source for each.
BEADS_DIR = ".beads"
DB_NAME = "beads.db"

#: The one suffix a board database may carry. Checked rather than appended:
#: appending would turn a mistyped directory into a new empty board named
#: after the typo, which is the mistake the check exists to prevent.
SUFFIX = ".db"

#: Where the overrides live in ``~/.claunch.yaml``.
SECTION = "beads"
BOARDS_KEY = "boards"


class BoardPathError(Exception):
    """An override that cannot be stored — not absolute, not a ``.db`` name,
    a directory, or a parent directory that is not there."""


@dataclass(frozen=True)
class BoardRef:
    """One board: what it is called, which file it is, and where ``br`` runs.

    ``root`` is the directory ``br`` is invoked in and the one a rebuild from
    a tracked ``issues.jsonl`` reads (``<root>/.beads/issues.jsonl``). For the
    default layout it is the workspace itself; for an override it is derived
    from the database's own location (:func:`root_of_db`), so a board moved
    elsewhere inside its repository still rebuilds from that repository's
    JSONL and a board moved outside one stands alone.
    """

    name: str
    db: str
    root: str
    #: The registered workspace this board belongs to, or ``""`` for
    #: :data:`DEFAULT_BOARD` and for a plain repository board.
    workspace: str = ""
    #: The path came from ``beads.boards`` rather than from the default rule.
    configured: bool = False

    @property
    def db_path(self) -> Path:
        return Path(self.db)

    @property
    def root_path(self) -> Path:
        return Path(self.root)

    def exists(self) -> bool:
        return Path(self.db).is_file()

    def to_dict(self) -> dict:
        return {
            "board": self.name,
            "db": self.db,
            "root": self.root,
            "workspace": self.workspace,
            "configured": self.configured,
            "exists": self.exists(),
        }


# --------------------------------------------------------------------------- #
# the registry
# --------------------------------------------------------------------------- #
def _section(doc: Optional[dict] = None) -> Dict[str, str]:
    doc = store.load() if doc is None else doc
    block = doc.get(SECTION)
    if not isinstance(block, dict):
        return {}
    boards = block.get(BOARDS_KEY)
    if not isinstance(boards, dict):
        return {}
    return {
        str(k): str(v)
        for k, v in boards.items()
        if isinstance(v, (str, os.PathLike)) and str(v).strip()
    }


def all_configured(doc: Optional[dict] = None) -> Dict[str, str]:
    """Every override, board name -> database path."""
    return dict(_section(doc))


def configured(name: str, doc: Optional[dict] = None) -> Optional[str]:
    """The database explicitly set for board ``name``, or ``None``."""
    return _section(doc).get(str(name or "").strip()) or None


def check_path(raw: str) -> str:
    """``raw`` as an absolute path to a ``.db`` file, or :class:`BoardPathError`.

    Four things are refused, and each one is a mistake that would otherwise
    surface as an empty board in an unexpected place: a relative path
    (resolved against whichever process read it), a name that does not end in
    ``.db`` (a directory typed where a file was asked for), a path that is
    itself a directory, and a parent directory that does not exist (``br
    init`` would fail, and the failure would arrive at the next board read
    rather than here).
    """
    text = str(raw or "").strip().strip('"')
    if not text:
        raise BoardPathError("a board needs a path to its .db file")
    candidate = Path(text).expanduser()
    if not candidate.is_absolute():
        raise BoardPathError(
            f"{text!r} is not an absolute path — name the .db file in full, "
            "for example D:\\boards\\gds6.db"
        )
    if candidate.suffix.lower() != SUFFIX:
        raise BoardPathError(
            f"{text!r} does not end in '{SUFFIX}' — this setting names the "
            f"database file itself, not the directory holding it (under that "
            f"directory it would be {candidate / BEADS_DIR / DB_NAME})"
        )
    if candidate.is_dir():
        raise BoardPathError(f"{candidate} is a directory, not a database file")
    parent = candidate.parent
    if not parent.is_dir():
        raise BoardPathError(
            f"no such directory: {parent} — create it first, or point the "
            "board at a .db file under a directory that exists"
        )
    try:
        return os.path.abspath(str(candidate))
    except OSError as exc:  # a path the OS will not normalise
        raise BoardPathError(f"unusable path {text!r}: {exc}") from exc


def set_db(name: str, raw: str) -> str:
    """Point board ``name`` at ``raw``; returns the path that was stored."""
    board = str(name or "").strip()
    if not board:
        raise BoardPathError("a board needs a name")
    path = check_path(raw)

    def _mutate(doc: dict) -> None:
        block = doc.get(SECTION)
        if not isinstance(block, dict):
            block = {}
            doc[SECTION] = block
        boards = block.get(BOARDS_KEY)
        if not isinstance(boards, dict):
            boards = {}
            block[BOARDS_KEY] = boards
        boards[board] = path

    store.update(_mutate)
    return path


def clear_db(name: str) -> bool:
    """Drop board ``name``'s override so it falls back to the default rule.

    The database file is not touched — this removes the registry entry only,
    the same contract ``claunch workspace rm`` has with the directory.
    """
    board = str(name or "").strip()
    removed = board in _section()

    def _mutate(doc: dict) -> None:
        block = doc.get(SECTION)
        if not isinstance(block, dict):
            return
        boards = block.get(BOARDS_KEY)
        if isinstance(boards, dict):
            boards.pop(board, None)
            if not boards:
                block.pop(BOARDS_KEY, None)
        if not block:
            doc.pop(SECTION, None)

    store.update(_mutate)
    return removed


# --------------------------------------------------------------------------- #
# path arithmetic
# --------------------------------------------------------------------------- #
def default_db_for(root: Path) -> Path:
    """The database a directory holds under the layout ``br init`` writes."""
    return Path(root) / BEADS_DIR / DB_NAME


def root_of_db(db: Path) -> Path:
    """The directory ``br`` should run in for a database at ``db``.

    A database inside a ``.beads/`` directory belongs to the checkout above
    it, which is what lets a board moved within its repository still rebuild
    from that repository's tracked JSONL. Anywhere else the database stands
    alone and its own directory is the root.
    """
    parent = Path(db).parent
    return parent.parent if parent.name == BEADS_DIR else parent


def workspace_ref(ws: workspaces.Workspace, doc: Optional[dict] = None) -> BoardRef:
    """The board belonging to a registered workspace."""
    raw = configured(ws.name, doc)
    if raw:
        db = Path(raw)
        return BoardRef(
            name=ws.name, db=str(db), root=str(root_of_db(db)),
            workspace=ws.name, configured=True,
        )
    return BoardRef(
        name=ws.name, db=str(default_db_for(Path(ws.path))), root=str(ws.path),
        workspace=ws.name, configured=False,
    )


def plain_ref(root: Path) -> BoardRef:
    """The board of a repository that is not a registered workspace.

    Named after its directory, because that is the only name it has. It keeps
    the old rule exactly, which is what stops a checkout that already holds a
    board from having its issues filed somewhere else.
    """
    root = Path(root)
    return BoardRef(
        name=root.name or str(root), db=str(default_db_for(root)),
        root=str(root), workspace="", configured=False,
    )


def default_ref(
    fallback_root: Optional[Path] = None, doc: Optional[dict] = None
) -> Optional[BoardRef]:
    """The :data:`DEFAULT_BOARD` board, or ``None`` when there is none.

    ``fallback_root`` is used only while the setting is unpinned; once
    :func:`ensure_default` has run there is a stored path and the answer no
    longer depends on which process is asking.
    """
    raw = configured(DEFAULT_BOARD, doc)
    if raw:
        db = Path(raw)
        return BoardRef(
            name=DEFAULT_BOARD, db=str(db), root=str(root_of_db(db)),
            workspace="", configured=True,
        )
    if fallback_root is None:
        return None
    root = Path(fallback_root)
    return BoardRef(
        name=DEFAULT_BOARD, db=str(default_db_for(root)), root=str(root),
        workspace="", configured=False,
    )


def ensure_default(root: Optional[Path]) -> Optional[str]:
    """Pin :data:`DEFAULT_BOARD` to ``root``'s database if it is not set yet.

    Called once at daemon startup with the board the daemon was already
    using, so the issues filed before workspaces had boards of their own keep
    reading as that board's without anything being copied or moved. Returns
    the path that is now stored, or ``None`` when nothing could be pinned.
    """
    existing = configured(DEFAULT_BOARD)
    if existing:
        return existing
    if root is None:
        return None
    db = str(default_db_for(Path(root)))

    def _mutate(doc: dict) -> None:
        block = doc.get(SECTION)
        if not isinstance(block, dict):
            block = {}
            doc[SECTION] = block
        boards = block.get(BOARDS_KEY)
        if not isinstance(boards, dict):
            boards = {}
            block[BOARDS_KEY] = boards
        boards.setdefault(DEFAULT_BOARD, db)

    store.update(_mutate)
    return configured(DEFAULT_BOARD)


def ref_for_root(
    root: Optional[Path], doc: Optional[dict] = None
) -> Optional[BoardRef]:
    """The board whose ``root`` is this directory.

    The inverse of the resolution in :func:`claude_launcher.cli_beads.resolve`,
    for the callers that already hold a root and need the database under it.
    The two must agree, so the rules are applied in the same order: a
    registered workspace at exactly this path owns it, then
    :data:`DEFAULT_BOARD` when its database sits here, then the plain
    repository rule.
    """
    if root is None:
        return None
    root = Path(root)
    doc = store.load() if doc is None else doc
    for ws in workspaces.list_all(doc):
        if same_path(ws.path, str(root)):
            return workspace_ref(ws, doc)
    fallback = default_ref(root, doc)
    if fallback is not None and same_path(fallback.root, str(root)):
        return fallback
    return plain_ref(root)


def db_for_root(root: Path, doc: Optional[dict] = None) -> Path:
    """The database file a root's board is kept in."""
    ref = ref_for_root(root, doc)
    return ref.db_path if ref is not None else default_db_for(Path(root))


def same_path(a: str, b: str) -> bool:
    """Whether two paths name the same file on this platform."""
    try:
        return os.path.normcase(os.path.abspath(str(a))) == os.path.normcase(
            os.path.abspath(str(b))
        )
    except (OSError, ValueError):
        return os.path.normcase(str(a)) == os.path.normcase(str(b))


def within(child: str, parent: str) -> bool:
    """Whether ``child`` is ``parent`` or a directory under it.

    Case-insensitive on Windows, like :func:`same_path`, and false when
    either path cannot be made absolute.
    """
    try:
        c = Path(os.path.normcase(os.path.abspath(str(child))))
        p = Path(os.path.normcase(os.path.abspath(str(parent))))
    except (OSError, ValueError):
        return False
    return c == p or p in c.parents


def prefix_for(name: str) -> str:
    """An issue prefix for a board being created for the first time.

    The board's own name, reduced to the alphabet ``br`` accepts for a
    prefix. The name is what the operator already calls this workspace, so
    the ids it mints read as belonging to it.
    """
    slug = re.sub(r"[^a-z0-9]+", "-", str(name or "").lower()).strip("-")
    return slug or "issue"


# --------------------------------------------------------------------------- #
# what the settings page reads
# --------------------------------------------------------------------------- #
def listing(fallback_root: Optional[Path] = None) -> List[dict]:
    """Every board the operator can point somewhere: one per registered
    workspace, plus :data:`DEFAULT_BOARD`.

    Each row carries the effective database, whether it came from a setting
    or from the default rule, whether the file is there, and the default the
    row would fall back to — so the page can offer "reset" without
    recomputing the rule in JavaScript. ``shared_with`` names the other
    boards resolving to the same file, which is expected for
    :data:`DEFAULT_BOARD` (it is pinned to a workspace's board) and a mistake
    anywhere else.
    """
    doc = store.load()
    rows: List[dict] = []
    default = default_ref(fallback_root, doc)
    if default is not None:
        rows.append({
            **default.to_dict(),
            "kind": "default",
            "path": default.root,
            "path_exists": Path(default.root).is_dir(),
            "default_db": str(default_db_for(Path(default.root))),
            "suggestions": suggestions(Path(default.root)),
        })
    for ws in workspaces.list_all(doc):
        ref = workspace_ref(ws, doc)
        rows.append({
            **ref.to_dict(),
            "kind": "workspace",
            "path": ws.path,
            "path_exists": ws.exists(),
            "default_db": str(default_db_for(Path(ws.path))),
            "suggestions": suggestions(Path(ws.path)),
        })
    by_db: Dict[str, List[str]] = {}
    for row in rows:
        by_db.setdefault(os.path.normcase(row["db"]), []).append(row["board"])
    for row in rows:
        peers = by_db.get(os.path.normcase(row["db"]), [])
        row["shared_with"] = [p for p in peers if p != row["board"]]
    return rows


def suggestions(root: Path) -> List[str]:
    """The ``.db`` files already sitting where this board would be.

    Offered to the settings field as completions, so the common case — a
    board that exists under another name, or in a directory beside the
    default one — is picked from a list rather than typed out.
    """
    found: List[str] = []
    for directory in (Path(root) / BEADS_DIR, Path(root)):
        try:
            if not directory.is_dir():
                continue
            for entry in sorted(directory.iterdir()):
                if entry.is_file() and entry.suffix.lower() == SUFFIX:
                    found.append(str(entry))
        except OSError:
            continue
    return found[:20]
