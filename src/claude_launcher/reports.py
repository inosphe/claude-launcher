"""Round reports: the HTML a session leaves behind when its round ends.

A cflow round's journal says what happened step by step, and a mesh message
says it to one reader once. Neither survives the terminal. A *report* is the
third thing: one local HTML file per round, written by the session that ran
it, indexed by the daemon under the session's name so the rail and the Beads
page can hand it back to a person long after the pane closed.

Three rules make that indexing possible without any registry to keep in sync:

* **One place.** ``<daemon dir>/reports/<session>/`` (:func:`dir_for`). It is
  outside every repository, so writing one never dirties a working tree, and
  outside ``sessions/<name>/``, so ``clear-sessions --logs`` does not take it.
* **One name.** ``<UTC stamp>-<issue>.html`` (:func:`filename`), which is the
  whole record: :func:`parse` reads the time and the issue back off the name,
  so a directory listing *is* the index. Nothing else has to be written down.
* **One check.** :func:`is_report` decides whether a file counts, and it is
  deliberately not "the path exists" — an empty file at the right path would
  satisfy a workflow's verify while telling a reader nothing.

The module is deliberately free of daemon imports beyond :mod:`paths`, because
both sides use it: ``claunch report`` writes through it and the daemon reads
through it.
"""

from __future__ import annotations

import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import List, Optional

from .daemon import paths

#: ``20260826T031200Z`` — sortable, filename-safe, and unambiguous about zone.
STAMP_FORMAT = "%Y%m%dT%H%M%SZ"

#: The name a report file must have to be indexed. Anything else in the
#: directory is ignored rather than guessed at.
FILENAME_RE = re.compile(r"^(\d{8}T\d{6}Z)-([A-Za-z0-9._-]+)\.html$")

#: Session names key the directory, so they must not be able to leave it.
SESSION_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,63}$")

#: Stands in for the issue when a round genuinely has none. A report is still
#: worth having; the name just says the board was not the source.
NO_ISSUE = "no-issue"

#: A file smaller than this is a stub, not a report. Chosen well under any
#: real page and well over an empty ``<html></html>`` — the point is to catch
#: "the path exists" passing for "the round was written up".
MIN_BYTES = 400


class ReportError(Exception):
    """A report could not be located, named or saved."""


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def stamp(at: Optional[datetime] = None) -> str:
    """The filename's time half, in UTC."""
    at = at or utcnow()
    if at.tzinfo is not None:
        at = at.astimezone(timezone.utc)
    return at.strftime(STAMP_FORMAT)


def issue_slug(issue: Optional[str]) -> str:
    """The filename's issue half — the id if it is usable as one.

    Board ids (``claunch-j31``) already are. Anything else is squeezed into
    the same alphabet rather than rejected, so a report is never lost to a
    naming argument; an empty result becomes :data:`NO_ISSUE`.
    """
    slug = re.sub(r"[^A-Za-z0-9._-]+", "-", (issue or "").strip()).strip("-._")
    return slug or NO_ISSUE


def filename(issue: Optional[str] = None, at: Optional[datetime] = None) -> str:
    """``<UTC stamp>-<issue>.html``."""
    return f"{stamp(at)}-{issue_slug(issue)}.html"


def parse(name: str) -> Optional[dict]:
    """Read a report filename back: ``{"at", "issue"}``, or ``None``.

    ``None`` is "this file is not a report", which is how a stray note or a
    browser's ``.html`` save sitting in the directory stays out of the index.
    """
    m = FILENAME_RE.match(name)
    if not m:
        return None
    try:
        at = datetime.strptime(m.group(1), STAMP_FORMAT).replace(tzinfo=timezone.utc)
    except ValueError:
        return None
    issue = m.group(2)
    return {
        "at": at.isoformat().replace("+00:00", "Z"),
        "issue": None if issue == NO_ISSUE else issue,
    }


def check_session(name: str) -> str:
    """Return ``name`` if it can key a report directory, else raise.

    The name arrives from a URL segment and from ``--session`` on the command
    line, and it becomes a path component — so it is validated here rather
    than trusted twice.
    """
    if not SESSION_RE.match(name or ""):
        raise ReportError(
            f"bad session name {name!r} (letters, digits, '-', '_', '.'; "
            "must start with a letter or digit, max 64 chars)"
        )
    return name


def dir_for(session: str, create: bool = False) -> Path:
    """One session's report directory."""
    path = paths.session_reports(check_session(session))
    if create:
        path.mkdir(parents=True, exist_ok=True)
    return path


def resolve(session: str, name: str) -> Path:
    """The path of one named report — for a route serving it.

    The name must match :data:`FILENAME_RE`, which admits no separators and no
    dots beyond the extension, so there is nothing left for a traversal to use.
    """
    if not FILENAME_RE.match(name or ""):
        raise ReportError(f"not a report filename: {name!r}")
    return dir_for(session) / name


def is_report(path: Path) -> bool:
    """Is this file a report a person could actually read?

    Three questions, and the last two are why this is not ``path.is_file()``:
    the name must be the indexed form, the file must carry an ``<html`` tag
    (a rendered page, not a stray text note), and it must be big enough to be
    a write-up rather than a placeholder left to satisfy a gate.
    """
    if not path.is_file() or not FILENAME_RE.match(path.name):
        return False
    try:
        if path.stat().st_size < MIN_BYTES:
            return False
        with path.open("rb") as fh:
            head = fh.read(4096).decode("utf-8", "replace").lower()
    except OSError:
        return False
    return "<html" in head


def entry(session: str, path: Path) -> dict:
    """One row of the index, as the API serves it."""
    meta = parse(path.name) or {"at": None, "issue": None}
    try:
        size = path.stat().st_size
    except OSError:
        size = 0
    return {
        "file": path.name,
        "at": meta["at"],
        "issue": meta["issue"],
        "size": size,
        "path": str(path),
        "url": f"/api/sessions/{session}/reports/{path.name}",
    }


def listing(session: str) -> List[dict]:
    """Every report a session has left, newest first.

    A bad session name or a missing directory is an empty list, not an error:
    the caller is drawing a panel, and "this session has no report yet" is a
    normal state that must not break the view around it.
    """
    try:
        base = dir_for(session)
    except ReportError:
        return []
    if not base.is_dir():
        return []
    files = [p for p in base.iterdir() if is_report(p)]
    files.sort(key=lambda p: p.name, reverse=True)
    return [entry(session, p) for p in files]


def latest(session: str) -> Optional[dict]:
    rows = listing(session)
    return rows[0] if rows else None


def names_in(session: str) -> List[str]:
    """Every correctly-named file in the directory, newest first.

    Wider than :func:`listing` on purpose — it does not ask whether the file
    is a *readable* report, only whether it holds one of the reserved names.
    :func:`target` needs exactly this: a stub half-written a minute ago is not
    a report, but its name is already taken by this round and handing out a
    second one would strand it.
    """
    try:
        base = dir_for(session)
    except ReportError:
        return []
    if not base.is_dir():
        return []
    names = [p.name for p in base.iterdir() if p.is_file() and FILENAME_RE.match(p.name)]
    return sorted(names, reverse=True)


def target(
    session: str,
    issue: Optional[str] = None,
    new: bool = False,
    at: Optional[datetime] = None,
) -> Path:
    """The path this round's report should be written to.

    Idempotent by default, and that is the point: an agent that asks twice —
    once to write the page and once after revising it — must get the same file
    both times, or a round ends up with two half-reports and a reader has to
    guess which is current. "The same" means same session and same issue; a
    round with a different issue is a different report. ``new`` forces a fresh
    stamp for the case where keeping the earlier one is deliberate.

    The match is on the *name*, not on :func:`listing`, and the difference is
    the common case rather than a corner: the first draft an agent writes may
    well not pass :func:`is_report` yet (too short, still being filled in),
    and if asking again then minted a second name, revising a draft would
    quietly leave the stub behind for a reader to find.
    """
    base = dir_for(session, create=True)
    if not new:
        slug = issue_slug(issue)
        for name in names_in(session):
            meta = parse(name)
            if meta and issue_slug(meta["issue"]) == slug:
                return base / name
    return base / filename(issue, at)


def save(
    session: str,
    source: Path,
    issue: Optional[str] = None,
    new: bool = False,
) -> Path:
    """Copy an HTML file written elsewhere into the indexed location."""
    source = Path(source)
    if not source.is_file():
        raise ReportError(f"no such file: {source}")
    dest = target(session, issue, new=new)
    if source.resolve() == dest.resolve():
        return dest
    shutil.copyfile(source, dest)
    return dest
