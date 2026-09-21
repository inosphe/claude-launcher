"""Projects: the tier above meshes and sessions.

A *project* is a name the user files meshes and sessions under. It exists
because one daemon now carries dozens of meshes and hundreds of sessions,
and a flat roster stops answering the question a person actually asks of
it — "the sessions for *this* piece of work". A project is that grouping:
every session and every mesh belongs to exactly one, and the listings
(``claunch sessions``, ``claunch mesh ls``, the web rail) can be narrowed to
one project.

Workspaces are **not** filed under projects. A workspace is metadata about a
directory on this machine (see :mod:`workspaces`); two projects may point at
the same one. What a project holds instead is a *default workspace*: the
directory a session created in that project starts in when nobody names one.
It is a default and nothing more — every creation path still takes an
explicit ``cwd``/``workspace``, and a session in the project is free to sit
anywhere.

The registry lives in ``~/.claunch.yaml`` under ``projects``, name -> fields::

    projects:
      launcher:
        default_workspace: claude-launcher   # a workspace NAME, or absent
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
from typing import Dict, List, Optional

from . import store, workspaces

#: The project every unfiled record belongs to.
DEFAULT = "default"

#: A project name: the same alphabet as a session or workspace name.
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]+$")


class ProjectError(Exception):
    """Raised for an unusable project name or a lookup that finds nothing."""


@dataclass(frozen=True)
class Project:
    name: str
    #: The workspace *name* new sessions start in by default, or ``None``.
    default_workspace: Optional[str] = None

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

    def to_dict(self, doc: Optional[dict] = None) -> dict:
        return {
            "name": self.name,
            "default_workspace": self.default_workspace,
            "default_cwd": self.default_cwd(doc),
            "is_default": self.is_default,
        }


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


def _from_entry(name: str, entry: dict) -> Project:
    ws = str(entry.get("default_workspace") or "").strip() or None
    return Project(name=name, default_workspace=ws)


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


def add(name: str, default_workspace: Optional[str] = None) -> Project:
    """Create a project, or update the default workspace of one that exists.

    Repeating the command is safe: an existing project with the same default
    is returned unchanged, and a different ``default_workspace`` replaces the
    setting rather than raising, because "make it so" is the only thing a
    person typing this twice can mean.
    """
    canon = check_name(name)
    ws = _check_workspace(default_workspace)
    existing = get(canon)
    if existing is not None and (default_workspace is None or existing.default_workspace == ws):
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
        block[canon] = entry

    store.update(_mutate)
    return Project(
        name=canon,
        default_workspace=ws if default_workspace is not None
        else (existing.default_workspace if existing else None),
    )


def set_default_workspace(name: str, workspace: Optional[str]) -> Project:
    """Set (or, with a blank value, clear) a project's default workspace."""
    project = require(name)
    return add(project.name, default_workspace=str(workspace or ""))


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
