"""Projects: the tier above meshes and sessions.

A *project* is a name the user files meshes and sessions under. It exists
because one daemon now carries dozens of meshes and hundreds of sessions,
and a flat roster stops answering the question a person actually asks of
it — "the sessions for *this* piece of work". A project is that grouping:
every session and every mesh belongs to exactly one, and the listings
(``claunch sessions``, ``claunch mesh ls --project``, the web rail) can be narrowed to
one project.

Workspaces are **not** filed under projects. A workspace is metadata about a
directory on this machine (see :mod:`workspaces`); two projects may point at
the same one. What a project holds instead is a *default workspace*: the
directory a session created in that project starts in when nobody names one.
It is a default and nothing more — every creation path still takes an
explicit ``cwd``/``workspace``, and a session in the project is free to sit
anywhere.

A project may also hold a *default mesh*: the mesh the create forms (the web
UI's new-session form, the wizard) pick in their Mesh row when that project
is chosen. Unlike the default workspace it has three states, because "nobody
has decided" and "decided: no mesh" are different answers:

- the key is absent: **unset** (:data:`UNSET`; the API leaves the field out,
  so a browser reads ``undefined``). Every project written before the field
  existed reads this way, and nothing on disk is migrated.
- ``default_mesh: null``: set to *no* mesh (``None``; the API sends ``null``).
- ``default_mesh: <name>``: a mesh name.

Both "no mesh" states leave the Mesh row empty; the distinction is kept so a
reader can tell a project nobody configured from one configured to stay out
of meshes. The CLI and API creation paths do not join it on their own: a
mesh join is asked for with ``--mesh``/``mesh`` as before.

The registry lives in ``~/.claude-launcher/.claunch.yaml`` under ``projects``, name -> fields::

    projects:
      launcher:
        default_workspace: claude-launcher   # a workspace NAME, or absent
        default_mesh: mesh-0826              # a mesh NAME, null, or absent
      hq:
        default_workspace: hq

The project named :data:`DEFAULT` always exists, whether or not the file lists
it: it is where every record written before projects existed is filed, so a
session or mesh with no ``project`` field reads as belonging to it and nothing
on disk has to be migrated. It cannot be removed.

Machine-local like ``workspaces``: a default workspace is a workspace name,
which only means something on the machine whose registry holds it.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Dict, List, Optional, Union

from . import store, workspaces

#: The project every unfiled record belongs to.
DEFAULT = "default"

#: A project name: the same alphabet as a session or workspace name.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ProjectError(Exception):
    """Raised for an unusable project name or a lookup that finds nothing."""


class _Unset:
    """The type of :data:`UNSET`: one instance, falsy, equal only to itself."""

    _instance = None

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance

    def __bool__(self) -> bool:
        return False

    def __repr__(self) -> str:
        return "UNSET"


#: A project's ``default_mesh`` when the registry has no key for it, distinct
#: from ``None`` (a project explicitly set to no default mesh).
UNSET = _Unset()

#: :func:`add`'s "leave the default mesh as it is"; never a value a project holds.
_KEEP = object()


@dataclass(frozen=True)
class Project:
    name: str
    #: The workspace *name* new sessions start in by default, or ``None``.
    default_workspace: Optional[str] = None
    #: The mesh *name* the create forms pick by default, ``None`` for "no
    #: mesh", or :data:`UNSET` when the registry records neither.
    default_mesh: Union[str, None, _Unset] = UNSET

    @property
    def is_default(self) -> bool:
        return self.name == DEFAULT

    def default_cwd(self, doc: Optional[dict] = None) -> Optional[str]:
        """The directory the default workspace points at, or ``None``.

        ``None`` both when no default is set and when the named workspace is
        no longer registered: a project keeps its setting when the workspace
        is unregistered, and the creation paths simply fall back to their own
        default (the caller's directory) until it is registered again.
        """
        if not self.default_workspace:
            return None
        ws = workspaces.get(self.default_workspace, doc)
        return ws.path if ws is not None else None

    @property
    def default_mesh_set(self) -> bool:
        """Whether the registry records a default mesh (a name *or* ``None``)."""
        return self.default_mesh is not UNSET

    def to_dict(self, doc: Optional[dict] = None) -> dict:
        out = {
            "name": self.name,
            "default_workspace": self.default_workspace,
            "default_cwd": self.default_cwd(doc),
            "is_default": self.is_default,
        }
        # Unset is the key's absence, on the wire as in the file: a reader
        # that has to tell it from ``null`` checks for the key.
        if self.default_mesh_set:
            out["default_mesh"] = self.default_mesh
        return out


def normalize(name) -> str:
    """The project a record's ``project`` field means: its own, or the default.

    The one place the "absent means default" rule is spelled, so a session
    record written before this module existed and a request that never
    mentions projects both file under :data:`DEFAULT`.
    """
    text = str(name or "").strip()
    return text or DEFAULT


def check_name(name) -> str:
    canon = str(name or "").strip()
    if not _NAME_RE.match(canon):
        raise ProjectError(
            f"invalid project name {name!r}: use letters, digits, '.', '_' or '-'"
        )
    return canon


def _section(doc: Optional[dict] = None) -> Dict[str, dict]:
    doc = store.load() if doc is None else doc
    block = doc.get("projects")
    if not isinstance(block, dict):
        return {}
    out: Dict[str, dict] = {}
    for key, value in block.items():
        name = str(key).strip()
        if not name:
            continue
        # A bare ``name:`` (null) is a project with no settings; anything
        # that is not a mapping is read the same way rather than refused, so
        # a hand-edited file never hides the projects around the odd entry.
        out[name] = dict(value) if isinstance(value, dict) else {}
    return out


def _mesh_from_entry(entry: dict):
    if "default_mesh" not in entry:
        return UNSET
    value = entry.get("default_mesh")
    if value is None:
        return None
    # A blank string in a hand-edited file reads as the explicit "no mesh"
    # it most plausibly means, rather than as a mesh named "".
    return str(value).strip() or None


def _from_entry(name: str, entry: dict) -> Project:
    ws = str(entry.get("default_workspace") or "").strip() or None
    return Project(
        name=name, default_workspace=ws, default_mesh=_mesh_from_entry(entry)
    )


def list_all(doc: Optional[dict] = None) -> List[Project]:
    """Every project, the default first and the rest name-sorted."""
    entries = _section(doc)
    rest = [_from_entry(n, e) for n, e in sorted(entries.items()) if n != DEFAULT]
    return [_from_entry(DEFAULT, entries.get(DEFAULT) or {})] + rest


def names(doc: Optional[dict] = None) -> List[str]:
    return [p.name for p in list_all(doc)]


def get(name, doc: Optional[dict] = None) -> Optional[Project]:
    """The project called ``name`` (blank = the default), or ``None``."""
    canon = normalize(name)
    entries = _section(doc)
    if canon == DEFAULT:
        return _from_entry(DEFAULT, entries.get(DEFAULT) or {})
    entry = entries.get(canon)
    return _from_entry(canon, entry) if entry is not None else None


def require(name, doc: Optional[dict] = None) -> Project:
    """Like :func:`get`, but an unknown name is an error naming the known ones."""
    found = get(name, doc)
    if found is None:
        known = ", ".join(names(doc))
        raise ProjectError(
            f"no project named {normalize(name)!r} (known: {known}) — "
            f"create it with 'claunch project add {normalize(name)}'"
        )
    return found


def default_cwd(name, doc: Optional[dict] = None) -> Optional[str]:
    """The default directory for sessions of project ``name``, or ``None``.

    Unknown projects answer ``None`` too: the creation paths ask this
    question *after* the project has been validated, and the listing paths
    ask it of records whose project may since have been removed.
    """
    found = get(name, doc)
    return found.default_cwd(doc) if found is not None else None


def _check_workspace(name: Optional[str]) -> Optional[str]:
    ws = str(name or "").strip()
    if not ws:
        return None
    if workspaces.get(ws) is None:
        known = ", ".join(w.name for w in workspaces.list_all()) or "none registered"
        raise ProjectError(
            f"no workspace named {ws!r} (known: {known}) — register it first "
            f"with 'claunch workspace add DIR --name {ws}'"
        )
    return ws


def check_mesh_name(name) -> str:
    """A default mesh's name, checked for shape only.

    Whether the mesh exists is the daemon's to answer (meshes live in its
    state, not in this file), and the API checks it before writing. A mesh
    removed later leaves the setting in place, like an unregistered default
    workspace, and the forms find no such option to pick.
    """
    canon = str(name or "").strip()
    # ``dev@pca`` is a mesh address (the mesh ``dev`` created on ``pca``).
    base, at, machine = canon.partition("@")
    if not _NAME_RE.match(base) or (at and not _NAME_RE.match(machine)):
        raise ProjectError(
            f"invalid mesh name {name!r}: use letters, digits, '.', '_' or '-' "
            "(optionally '@<machine>')"
        )
    return canon


def add(
    name: str,
    default_workspace: Optional[str] = None,
    default_mesh=_KEEP,
) -> Project:
    """Create a project, or update the defaults of one that exists.

    Repeating the command is safe: an existing project with the same default
    is returned unchanged, and a different ``default_workspace`` replaces the
    setting rather than raising, because "make it so" is the only thing a
    person typing this twice can mean.

    ``default_mesh`` takes a mesh name, ``None`` (explicitly no mesh) or
    :data:`UNSET` (drop the key). Left out, the setting is not touched, so a
    new project starts unset.
    """
    canon = check_name(name)
    ws = _check_workspace(default_workspace)
    mesh = default_mesh
    if mesh is not _KEEP and mesh is not None and mesh is not UNSET:
        mesh = check_mesh_name(mesh)
    existing = get(canon)
    if (
        existing is not None
        and (default_workspace is None or existing.default_workspace == ws)
        and (mesh is _KEEP or existing.default_mesh == mesh)
    ):
        return existing

    def _mutate(doc: dict) -> None:
        block = doc.get("projects")
        if not isinstance(block, dict):
            block = {}
            doc["projects"] = block
        entry = block.get(canon)
        if not isinstance(entry, dict):
            entry = {}
        if default_workspace is not None:
            if ws:
                entry["default_workspace"] = ws
            else:
                entry.pop("default_workspace", None)
        if mesh is UNSET:
            entry.pop("default_mesh", None)
        elif mesh is not _KEEP:
            entry["default_mesh"] = mesh
        block[canon] = entry

    store.update(_mutate)
    if mesh is _KEEP:
        mesh = existing.default_mesh if existing else UNSET
    return Project(
        name=canon,
        default_workspace=ws if default_workspace is not None
        else (existing.default_workspace if existing else None),
        default_mesh=mesh,
    )


def set_default_workspace(name: str, workspace: Optional[str]) -> Project:
    """Set (or, with a blank value, clear) a project's default workspace."""
    project = require(name)
    return add(project.name, default_workspace=str(workspace or ""))


def set_default_mesh(name: str, mesh) -> Project:
    """Set a project's default mesh: a name, ``None`` for "no mesh", or
    :data:`UNSET` to drop the setting back to never-configured."""
    project = require(name)
    return add(project.name, default_mesh=mesh)


def remove(name: str) -> Project:
    """Drop a project from the registry. Its sessions and meshes are untouched.

    The records keep their ``project`` field: they still say where they were
    filed, and the listings show that name even though the registry no longer
    knows it. The default project cannot be removed — it is where the unfiled
    records live, and there would be nowhere for them to go.
    """
    canon = normalize(name)
    if canon == DEFAULT:
        raise ProjectError(
            f"the {DEFAULT!r} project cannot be removed — it is where every "
            "session and mesh that names no project is filed"
        )
    found = require(canon)

    def _mutate(doc: dict) -> None:
        block = doc.get("projects")
        if isinstance(block, dict):
            block.pop(canon, None)
            if not block:
                doc.pop("projects", None)

    store.update(_mutate)
    return found


def matches(record_project, wanted) -> bool:
    """Whether a record whose ``project`` field is ``record_project`` is in
    project ``wanted``. Blank ``wanted`` means "every project" — the filter
    a listing applies when nobody narrowed it."""
    if not str(wanted or "").strip():
        return True
    return normalize(record_project) == normalize(wanted)


# --------------------------------------------------------------------------- #
# the project a managed session stands in, and the scope its listings take
# --------------------------------------------------------------------------- #

#: Exported into every managed session's environment next to
#: ``CLAUNCH_SESSION``: the project the session is filed under (always a
#: name — the default project is spelled out, never left blank).
SESSION_ENV = "CLAUNCH_PROJECT"

#: The ``--project`` value that means "every project" on a listing. Inside
#: a managed session a listing narrows itself to the session's own project,
#: and this word is how an agent widens it on purpose.
ALL = "all"


@dataclass(frozen=True)
class ListingScope:
    """What a session/mesh listing shows, and why.

    ``project`` is the filter to apply (blank = every project). ``own`` is
    true when the filter was not asked for but taken from the calling managed
    session — the case that earns a footer telling the reader what was hidden
    and how to widen the view, because that reader is an agent that would
    otherwise take the narrowed list for the whole roster (or, before this
    existed, the whole roster for its own project: 2026-09-24, s769).
    """

    project: str
    own: bool = False


def listing_scope(asked, *, environ, fetch_own=None) -> ListingScope:
    """The project a listing narrows to.

    - ``asked`` (the ``--project`` flag) wins: a name narrows to it,
      :data:`ALL` widens to every project.
    - Otherwise, inside a managed session (``CLAUNCH_SESSION`` set) the
      scope is the session's own project: :data:`SESSION_ENV` when the
      daemon exported it, else ``fetch_own(session_name)`` — a lookup of
      the session's record, for sessions launched by a daemon older than
      the variable. A lookup that fails or answers nothing leaves the
      scope at the default project rather than at everything: a session
      whose record cannot be read is filed under the default, and widening
      silently is the failure this exists to prevent.
    - Outside a managed session nothing narrows, as the listings always
      behaved for a person at their own shell.
    """
    text = str(asked or "").strip()
    if text:
        return ListingScope("" if text == ALL else text)
    session = str(environ.get("CLAUNCH_SESSION") or "").strip()
    if not session:
        return ListingScope("")
    own = str(environ.get(SESSION_ENV) or "").strip()
    if not own and fetch_own is not None:
        try:
            own = str(fetch_own(session) or "").strip()
        except Exception:  # noqa: BLE001 — the lookup is a convenience, not a gate
            own = ""
    return ListingScope(normalize(own), own=True)


def _plural(noun: str) -> str:
    """'session' -> 'sessions', 'mesh' -> 'meshes' (the two nouns the
    listings use; a sibilant ending takes -es)."""
    return noun + ("es" if noun.endswith(("s", "x", "z", "sh", "ch")) else "s")


def scope_footer(scope: ListingScope, *, hidden: int, noun: str, command: str) -> str:
    """The one line a session-scoped listing ends with, or ``""``.

    Always printed when the scope came from the session, hidden rows or not:
    the reader has to learn the list was narrowed even when nothing was
    left out, or a later read with rows hidden looks the same as this one.
    """
    if not scope.own:
        return ""
    tail = (
        f"{hidden} {noun}(s) in other projects not shown"
        if hidden else f"no {_plural(noun)} in other projects"
    )
    return (
        f"project: {scope.project} (this session's) -- {tail}; "
        f"'{command} --project {ALL}' lists every project"
    )
